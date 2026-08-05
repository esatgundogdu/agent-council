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

from collections import deque
from datetime import datetime, timezone
from pathlib import Path

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

        self.written = 0
        self.out_bytes = 0
        self.err_bytes = 0
        self.skipped = 0
        self.truncated = False
        #: Lines held back once the cap is hit, so the end survives the middle.
        self._tail: deque[str] = deque()
        self._tail_bytes = 0
        self._handle = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, argv: list[str], cwd: str) -> None:
        """Open the file and write the header. Called once the command is final —
        after `resolve_binary`, so the path recorded is the one actually started."""
        if self._handle is not None or self._closed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8", errors="replace", newline="")
        except OSError:
            self._handle = None
            self._closed = True
            return
        self._raw(_header(self, argv, cwd))

    def write(self, stream: str, line: str) -> None:
        """Record one line. `stream` is "out" or "err"."""
        if stream == "err":
            self.err_bytes += len(line)
            line = STDERR_PREFIX + line
        else:
            self.out_bytes += len(line)
        if self._handle is None:
            return
        text = line if line.endswith("\n") else line + "\n"
        if self.written + len(text) <= self.limit:
            self._raw(text)
            return
        # Past the cap: keep writing into a bounded tail instead, so the end of the
        # call survives and only the middle is lost.
        self.truncated = True
        self._tail.append(text)
        self._tail_bytes += len(text)
        while self._tail_bytes > self.tail_bytes and len(self._tail) > 1:
            self.skipped += len(self._tail.popleft())
            self._tail_bytes = sum(len(item) for item in self._tail)

    def finish(self, exit_code: int | None, seconds: float, error: str = "") -> None:
        """Flush the tail, write the footer, close. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._handle is None:
            return
        if self.truncated:
            self._raw(
                f"\n#  … {self.skipped:,} bytes omitted here "
                f"({self.limit // (1024 * 1024)} MiB cap; the end follows) …\n\n"
            )
            for line in self._tail:
                self._raw(line)
        self._raw(_footer(self, exit_code, seconds, error))
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    # -- reporting ---------------------------------------------------------

    @property
    def bytes_seen(self) -> int:
        """Everything the process printed, whether or not it fitted."""
        return self.out_bytes + self.err_bytes

    def _raw(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(text)
            self._handle.flush()
        except (OSError, ValueError):
            # ValueError: the handle was closed under us. Either way, stop trying —
            # a log that cannot be written must not turn into an exception per line.
            self._handle = None
            return
        self.written += len(text)


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
