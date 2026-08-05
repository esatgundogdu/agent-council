"""A verbatim record of what one harness process printed.

Everything else this package keeps about a turn has been through a parser: `turn_end`
holds the envelope's `comment`, `turn_delta` holds a tool name and one truncated
argument, and the adapters overwrite the raw stdout they were handed the moment they
have extracted an answer from it. That is the right shape for a transcript and the
wrong shape for a diagnosis — when a panelist times out, exits non-zero or returns
nothing at all, the parsed view has by construction thrown away the only evidence.

So the raw bytes are teed here on their way past, before any parsing, and land in
`<session>/calls/`. That directory is inside the session, so deleting a session takes
its call logs with it and nothing has to remember to clean up.

The file is a console log, not a data format: stdout goes down verbatim so a JSONL
stream stays re-parsable and copy-pastable, and only stderr is marked.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

#: How much of one call is kept. A codex turn under `--json` prints a few hundred
#: kilobytes; a harness stuck in a loop prints until it is killed.
MAX_CALL_LOG_BYTES = 2 * 1024 * 1024

#: When the cap is hit, this much of the *end* is kept as well as the beginning.
#: Both ends matter and for different reasons: the head has the command line and the
#: first thing that went wrong, the tail has whatever it was doing when it died.
TAIL_BYTES = 32 * 1024

#: No argument is worth more than this in a header. On platforms where opencode takes
#: its prompt positionally the whole prompt would otherwise be here — and it is already
#: in `stream.jsonl`, in full, exactly once.
MAX_ARG_CHARS = 400

#: How stale the file on disk may get. A reader polls a running log every few seconds,
#: so this only has to be well inside that — and flushing per line put a syscall per
#: line of every harness's output on the event loop all the panelists share.
FLUSH_SECONDS = 0.25
FLUSH_BYTES = 8 * 1024

STDERR_PREFIX = "[stderr] "


def call_filename(seq: int, agent: str, round_no: int) -> str:
    """The name one call's log gets.

    `seq` counts calls across the whole session rather than turns, because a turn can
    make two: a resumed call whose session is refused falls back to a cold one for the
    same round, and that retry is often the interesting half.

    Must keep matching `CALL_FILE` in `council/server/app.py`, which is what stops the
    HTTP route reading arbitrary files.
    """
    return f"{seq:04d}-{agent.lower().replace('_', '-')}-r{round_no}.log"


class CallLog:
    """One harness process, from its command line to its exit code.

    Written as it happens rather than at the end: the calls worth reading are the ones
    that never reach an end, and a live turn's log is readable while it runs.

    Nothing here may raise into a turn. A full disk is a reason to lose the log, not
    the council — every failure goes inert and the run continues without it.
    """

    def __init__(
        self,
        path: Path,
        *,
        agent: str = "",
        phase: int | None = None,
        round_no: int | None = None,
        model: str | None = None,
        effort: str | None = None,
        session: str | None = None,
        limit: int = MAX_CALL_LOG_BYTES,
        tail_bytes: int = TAIL_BYTES,
        on_open: Callable[[], None] | None = None,
    ) -> None:
        self.path = path
        self.agent = agent
        self.phase = phase
        self.round_no = round_no
        self.model = model
        self.effort = effort
        self.session = session
        self.limit = limit
        self.tail_bytes = tail_bytes
        #: Announces the file the moment it exists. The caller cannot do this itself:
        #: it does not get control back between starting the process and the call
        #: ending, which for a Phase 1 read of a large repository is fifteen minutes —
        #: precisely the stretch somebody would want to look at the log.
        self.on_open = on_open

        self.written = 0
        self.out_bytes = 0
        self.err_bytes = 0
        self.skipped = 0
        self.truncated = False
        #: Lines held back once the cap is hit, so the end survives the middle. Bytes,
        #: not text: every budget here is in bytes, and a deque of `str` made the cap,
        #: the tail and the reported size all count code points instead — wrong by up
        #: to 4x for any panelist that printed non-ASCII, which is any of them.
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._handle = None
        self._closed = False
        self._unflushed = 0
        self._flushed_at = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    def start(self, argv: list[str], cwd: str) -> None:
        """Open the file and write the header. Called once the command is final —
        after `resolve_binary`, so the path recorded is the one actually started."""
        if self._handle is not None or self._closed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Binary, so that every count in this class is a byte count by construction
            # rather than by remembering to encode at each site.
            self._handle = self.path.open("wb")
        except OSError:
            self._handle = None
            self._closed = True
            return
        self._raw(_header(self, argv, cwd))
        # Only once the header is actually on disk. `open("w")` succeeding leaves a
        # zero-byte file, so announcing a log whose first write failed produces a
        # readable-but-empty file that `finish` then refuses to complete — an entry
        # the UI shows as running for ever, on a screen whose job is saying which
        # calls are.
        if self.on_open is not None and self.written:
            try:
                self.on_open()
            except Exception:  # noqa: BLE001 - announcing a log is never worth a turn
                pass

    def write(self, stream: str, line: str) -> None:
        """Record one line. `stream` is "out" or "err"."""
        # Encoded, not `len(str)`. Every quantity here is named and documented as bytes,
        # and the cap is sold as a hard one — but a panelist is free to print prose in
        # any language, box-drawing characters or emoji, and counting code points let
        # the file on disk run past the nominal 2 MiB by up to 4x while the footer and
        # the size shown in the UI were wrong by the same factor. Found by a real panel
        # reviewing this feature; the existing tests could not catch it because they
        # only ever wrote ASCII, where the two counts coincide.
        size = len(line.encode("utf-8", "replace"))
        if stream == "err":
            self.err_bytes += size
            line = STDERR_PREFIX + line
        else:
            self.out_bytes += size
        if self._handle is None:
            return
        text = line if line.endswith("\n") else line + "\n"
        raw = text.encode("utf-8", "replace")
        # `not self.truncated` is load-bearing. Without it a short line arriving after
        # the cap was hit still fits under `limit` and goes back into the head — landing
        # in the file *before* content that was written to the tail earlier. A console
        # log whose lines are out of order is worse than one that is short.
        if not self.truncated and self.written + len(raw) <= self.limit:
            self._write(raw)
            return
        if not self.truncated:
            self._begin_tail()
        self._tail.append(raw)
        self._tail_bytes += len(raw)
        self._trim_tail()

    def _begin_tail(self) -> None:
        """Say in the file itself that the cap has been reached.

        A reader watching a live log otherwise sees it simply stop growing, with no way
        to tell a flooding harness from a hung one — which is the exact distinction they
        opened it to make.
        """
        self.truncated = True
        self._raw(
            f"\n#{'─' * 76}\n"
            f"# The {self.limit // (1024 * 1024)} MiB cap was reached here. The call is\n"
            f"# still running and still printing; the last {self.tail_bytes // 1024} kB of\n"
            f"# what follows is being held and is appended when it ends.\n"
            f"#{'─' * 76}\n\n"
        )
        # Past the throttle, deliberately. This is the one line whose whole purpose is
        # to reach a reader immediately — it is what stops a live view of a flooding
        # harness from looking identical to a hung one — and it happens once per call.
        self._flush()

    def _trim_tail(self) -> None:
        """Keep the tail inside its budget, newest content first.

        Trims the front of the oldest entry rather than dropping it whole. stdout and
        stderr are teed in from two independently scheduled readers, so "oldest" is not
        "least valuable": a stray stderr line arriving after the harness's terminal
        event would otherwise evict that event outright. Degrading it to its own last
        bytes keeps the part that names the outcome.

        A terminal event larger than `tail_bytes` still cannot be kept whole. That is
        the honest bound of a fixed budget, and the footer reports what was dropped.
        """
        while self._tail_bytes > self.tail_bytes and self._tail:
            excess = self._tail_bytes - self.tail_bytes
            oldest = self._tail[0]
            if len(oldest) <= excess:
                self._tail.popleft()
                self._tail_bytes -= len(oldest)
                self.skipped += len(oldest)
            else:
                # Forward to a character boundary. A byte-exact cut can land inside a
                # multi-byte sequence and leave a replacement glyph at the seam, and
                # this file is read as UTF-8 by everything downstream — the cost of
                # keeping it valid is at most three bytes of slack.
                cut = excess
                while cut < len(oldest) and (oldest[cut] & 0xC0) == 0x80:
                    cut += 1
                self._tail[0] = oldest[cut:]
                self._tail_bytes -= cut
                self.skipped += cut

    def finish(self, exit_code: int | None, seconds: float, error: str = "") -> None:
        """Flush the tail, write the footer, close. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._handle is None:
            return
        if self.truncated:
            self._raw(f"#  … {self.skipped:,} bytes dropped from the middle …\n\n")
            for chunk in self._tail:
                self._write(chunk)
        self._raw(_footer(self, exit_code, seconds, error))
        try:
            self._handle.flush()
            self._handle.close()
        except (OSError, ValueError):
            pass
        self._handle = None

    def note(self, label: str, text: str) -> None:
        """Append something learned after the process had already exited.

        Every adapter can turn a clean exit into a failed turn: a harness that returns
        nothing usable exits 0, and the reply is rejected afterwards. By then this file
        is closed saying `exit code: 0` and no error — which is true of the process and
        false about the call, and the reader has no way to tell.

        Appended rather than folded into the footer, deliberately. The exit code stays
        the process's own; this is the council's verdict on what it produced, and
        conflating the two makes the log lie in the other direction.
        """
        if not text.strip() or not self.written:
            return
        try:
            with self.path.open("ab") as handle:
                handle.write(
                    f"# {label:<9}: {text.strip()[:2000]}\n".encode("utf-8", "replace")
                )
        except OSError:
            pass

    # -- reporting ---------------------------------------------------------

    @property
    def bytes_seen(self) -> int:
        """Everything the process printed, whether or not it fitted."""
        return self.out_bytes + self.err_bytes

    def _raw(self, text: str) -> None:
        self._write(text.encode("utf-8", "replace"))

    def _write(self, raw: bytes) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(raw)
        except (OSError, ValueError):
            # ValueError: the handle was closed under us. Either way, stop trying —
            # a log that cannot be written must not turn into an exception per line.
            self._handle = None
            return
        self.written += len(raw)
        self._unflushed += len(raw)
        # Throttled rather than per line. Every panelist's pump shares one event loop,
        # so a flush per line is a syscall per line of every harness's output on the
        # thread that is also delivering everyone else's deltas — and the file only has
        # to be current enough for a reader polling it every three seconds.
        if (
            self._unflushed >= FLUSH_BYTES
            or time.monotonic() - self._flushed_at >= FLUSH_SECONDS
        ):
            self._flush()

    def _flush(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.flush()
        except (OSError, ValueError):
            self._handle = None
            return
        self._unflushed = 0
        self._flushed_at = time.monotonic()


class _Discard(CallLog):
    """A CallLog that records nothing, for `capture_console: false`.

    A null object rather than a `None` check at four call sites, because three of those
    are inside `run_process`'s pump loop where a branch per line is the wrong shape.
    """

    def __init__(self) -> None:
        super().__init__(Path("."))
        self._closed = True

    def start(self, argv: list[str], cwd: str) -> None:
        return

    def write(self, stream: str, line: str) -> None:
        return

    def finish(self, exit_code: int | None, seconds: float, error: str = "") -> None:
        return


#: Shared, stateless, and never written to.
DISCARD = _Discard()


def _header(log: CallLog, argv: list[str], cwd: str) -> str:
    lines = [
        "# council call log — everything this harness process printed, unparsed.",
        f"# {STDERR_PREFIX.strip()} marks a line that came from stderr; the rest is stdout, verbatim.",
        "#",
        f"# agent    : {log.agent or '—'}",
        f"# phase    : {log.phase if log.phase is not None else '—'}",
        f"# round    : {log.round_no if log.round_no is not None else '—'}",
        f"# model    : {log.model or '(harness default)'}",
        f"# effort   : {log.effort or '(harness default)'}",
        f"# resuming : {log.session or 'no — cold start'}",
        f"# started  : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# cwd      : {cwd}",
        f"# command  : {_command(argv)}",
        "#" + "─" * 76,
        "",
    ]
    return "\n".join(lines)


def _footer(log: CallLog, exit_code: int | None, seconds: float, error: str) -> str:
    lines = [
        "",
        "#" + "─" * 76,
        f"# exit code: {'killed' if exit_code is None else exit_code}",
        f"# duration : {seconds:.1f}s",
        f"# stdout   : {log.out_bytes:,} bytes",
        f"# stderr   : {log.err_bytes:,} bytes",
    ]
    if log.truncated:
        lines.append(f"# omitted  : {log.skipped:,} bytes from the middle")
    if error:
        lines.append(f"# error    : {error.strip()[:2000]}")
    return "\n".join(lines) + "\n"


def _command(argv: list[str]) -> str:
    """The command line, quoted the way a shell would need it."""
    return " ".join(_quote(_clip(arg)) for arg in argv)


def _clip(arg: str) -> str:
    return arg if len(arg) <= MAX_ARG_CHARS else f"{arg[:MAX_ARG_CHARS]}… (+{len(arg) - MAX_ARG_CHARS} chars)"


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or "\t" in arg) else arg
