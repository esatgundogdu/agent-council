"""Uniform subprocess interface to every agent harness.

All panelists are CLI agents. The orchestrator only ever does: send a prompt, watch it
work, get a final message back. Everything hard (tool loops, file reading, context
handling) lives inside the harness.

Output is consumed **incrementally**. Every harness we drive already emits
line-delimited JSON as it works, so a caller that passes `on_delta` sees text, tool
activity and token usage while the turn is still running — which is what makes a live
UI possible at all. Callers that pass nothing get the same buffered behaviour as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import glob
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Callable

from ..calls import DISCARD, CallLog

# A harness that floods stdout is a bug, not a reason to exhaust memory.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: A "line" longer than this is flushed unterminated rather than buffered forever.
#: Harness JSONL lines routinely exceed asyncio's default 64 KiB StreamReader limit —
#: which is why stdout is chunk-read and split here instead of via readline().
MAX_LINE_BYTES = 4 * 1024 * 1024

READ_CHUNK = 64 * 1024

#: How long the readers get, after the process has ended, to finish what is still in
#: the pipes. Generous for a dead child, short enough that a grandchild holding the
#: write end open cannot stall the turn after it.
DRAIN_SECONDS = 10.0

WINDOWS = os.name == "nt"

#: ERROR_FILENAME_EXCED_RANGE. Windows caps the whole command line at 32767 characters
#: and reports the overflow as this, which reaches Python as FileNotFoundError(errno=2).
_WIN_COMMAND_LINE_TOO_LONG = 206


@dataclass
class Delta:
    """One increment of a turn in progress, normalised across harnesses.

    kind:
      ``text``     assistant prose, incremental
      ``tool``     the panelist used a tool (`tool` = name, `target` = file/argument)
      ``usage``    token counts, as the harness reports them
      ``session``  the harness conversation id, as soon as it is known
      ``status``   coarse harness state ("requesting", "thinking", ...)
    """

    kind: str
    text: str = ""
    tool: str = ""
    target: str = ""
    tokens: int | None = None
    session_id: str | None = None


#: What adapters hand back to the caller while a turn is in flight.
DeltaSink = Callable[[Delta], None]


class LineParser:
    """Turns harness stdout lines into deltas. One instance per call, so it may
    hold whatever cross-line state its harness needs (streamed part ids, etc.)."""

    def feed(self, line: str) -> list[Delta]:  # pragma: no cover - overridden
        return []


@dataclass
class Reply:
    ok: bool
    text: str = ""
    error: str = ""
    exit_code: int | None = None
    duration: float = 0.0
    stderr: str = ""
    tokens: int | None = None
    # The harness-side conversation this reply belongs to. Passing it back on the next
    # call keeps the panelist's own exploration context instead of starting cold.
    session_id: str | None = None
    meta: dict = field(default_factory=dict)


class AdapterError(Exception):
    """Raised for misconfiguration that no retry can fix (e.g. missing binary)."""


class Adapter:
    """Base class: build a command line, run it, extract the final message."""

    name = "base"

    def __init__(
        self,
        model: str | None = None,
        variant: str | None = None,
        effort: str | None = None,
        **kwargs,
    ):
        self.model = model
        self.variant = variant
        #: `low`…`max`, or None to leave the harness on its own default. Only the
        #: harnesses that have such a control read it; see `config.EFFORT_ADAPTERS`.
        self.effort = effort


    def new_parser(self) -> LineParser:
        """A fresh line parser for one call. Default: no live output."""
        return LineParser()

    async def ask(
        self,
        prompt: str,
        cwd: str,
        timeout: int,
        session: str | None = None,
        on_delta: DeltaSink | None = None,
        call_log: CallLog | None = None,
    ) -> Reply:
        """Send a prompt. With `session`, continue that conversation instead of a new one."""
        raise NotImplementedError

    def _line_sink(self, on_delta: DeltaSink | None):
        """Wire this adapter's parser to a caller's delta sink.

        Returns the `on_line` callback for `run_process`, or None when nobody is
        listening — in which case stdout is merely accumulated, as before.
        """
        if on_delta is None:
            return None
        parser = self.new_parser()

        def on_line(line: str) -> None:
            # Delivery is inside the guard too. It used to sit outside, so a parser
            # bug was survivable but a *sink* failure — the event log hitting a full
            # disk, say — escaped through `_pump` and out of `run_process`, where only
            # the timeout path kills the child. The turn died and the harness kept
            # running, still spending tokens, with nobody reading it.
            try:
                for delta in parser.feed(line):
                    on_delta(delta)
            except Exception:  # noqa: BLE001 - live output is never worth the turn
                return

        return on_line


def resolve_binary(name: str) -> str:
    """The name of a harness executable in the form the OS can actually start.

    Windows needs this. `CreateProcess` only ever appends `.exe`, so a bare "codex"
    never finds the `codex.cmd` that npm installs — every such panelist would be
    dropped as "not found" despite being installed and authenticated. `shutil.which`
    applies PATHEXT and returns the real path. An unresolvable name is handed back
    unchanged so the failure surfaces as the usual "executable not found".

    A path may be a glob, and has to be able to be: the ChatGPT desktop app installs
    codex under a content-hashed directory — `.../Codex/bin/69066b736e1e17a4/codex.exe`
    — and mints a new hash on every update. That directory is on nobody's PATH, so it
    has to be named, and naming it literally is a config line that breaks the next time
    the app updates itself. `.../Codex/bin/*/codex.exe` keeps working.
    """
    if _is_glob(name):
        return _newest_match(name)
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name
    return shutil.which(name) or name


def _is_glob(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def _newest_match(pattern: str) -> str:
    """The most recently modified file a glob matches.

    Newest rather than last-sorted: hashes have no meaningful order, and after an
    update the old directory usually still exists. The pattern is handed back when
    nothing matches, so the error names what was looked for rather than a stray "".
    """
    matches = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not matches:
        return pattern
    return max(matches, key=lambda p: os.stat(p).st_mtime)


def _start_error(argv: list[str], exc: OSError) -> AdapterError:
    """Explain a failed process start in terms of its actual cause.

    Windows reports an over-long command line as FileNotFoundError(errno=2), which is
    indistinguishable from a missing binary unless the winerror is checked first — so
    the length case is tested before anything else.
    """
    binary = argv[0]
    too_long = (
        getattr(exc, "winerror", None) == _WIN_COMMAND_LINE_TOO_LONG
        or getattr(exc, "errno", None) == errno.E2BIG
    )
    if too_long:
        size = sum(len(arg) + 1 for arg in argv)
        return AdapterError(
            f"command line too long for {binary} ({size} characters). The prompt is "
            "what overflows it: lower protocol.compaction_threshold."
        )
    if isinstance(exc, FileNotFoundError):
        return AdapterError(
            f"harness executable not found: {binary}. It is looked up on the PATH of "
            "whatever process is running the council — so if you installed it after "
            "starting the daemon, that daemon still has the old PATH: "
            "`council down && council up`."
        )
    return AdapterError(f"could not start {binary}: {exc}")


async def run_process(
    argv: list[str],
    cwd: str,
    timeout: int,
    stdin_data: str | None = None,
    env: dict | None = None,
    on_line: Callable[[str], None] | None = None,
    call_log: CallLog | None = None,
) -> Reply:
    """Run a harness process to completion, killing it and its children on timeout.

    stdout is drained in chunks and split into lines as they arrive; with `on_line`
    each complete line is handed over immediately. Draining never stops, even once
    the output cap is hit, because a child whose pipe fills up blocks forever.

    `call_log` is teed both streams verbatim, before anything parses them. It is the
    only copy that survives — every adapter overwrites `Reply.text` with the answer it
    extracted, and `Reply.stderr` is read by nobody.

    stdin is always either fed `stdin_data` and closed, or connected to /dev/null:
    a harness left with an open inherited stdin can block waiting for input.
    """
    started = time.monotonic()
    full_env = {**os.environ, **(env or {})}
    argv = [resolve_binary(argv[0]), *argv[1:]]
    log = call_log or DISCARD
    # After `resolve_binary`, so the header names the executable actually started —
    # on Windows that is the `.cmd` shim's real path, which is half the diagnosis.
    log.start(argv, cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=full_env,
            stdin=asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # POSIX: its own session, so the whole group can be killed on timeout.
            # Windows ignores this; _terminate_tree walks the child tree there instead.
            start_new_session=True,
        )
    except OSError as exc:
        error = _start_error(argv, exc)
        log.finish(None, time.monotonic() - started, str(error))
        raise error from exc

    feed = asyncio.create_task(_feed_stdin(proc, stdin_data))
    out_task = asyncio.create_task(_pump(proc.stdout, on_line, log, "out"))
    err_task = asyncio.create_task(_pump(proc.stderr, None, log, "err"))

    async def finish_reading() -> tuple[str, str]:
        """Let both pumps run to EOF and hand back everything they read."""
        return await asyncio.gather(out_task, err_task)

    timed_out = False
    try:
        # The deadline is on the *process*, not on the reading.
        #
        # It used to wrap the whole thing, so a timeout cancelled the pumps — and a
        # cancelled pump loses two things at once: the bytes still in the pipe, and its
        # own accumulated buffer, which is what every adapter parses. Killing the child
        # instead makes both pipes hit EOF, and the pumps then finish on their own with
        # everything the process managed to print. A timed-out turn's log is the only
        # record that turn leaves; it should be the whole one.
        #
        # No deadlock risk in waiting on the process while it still holds full pipes:
        # the pumps are draining them concurrently, which is the reason that hazard
        # exists and the reason this is safe.
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill(proc)
        # Bounded: a grandchild holding the write end open would otherwise keep both
        # pipes from ever reaching EOF, and this call would never return.
        out, err = await asyncio.wait_for(finish_reading(), timeout=DRAIN_SECONDS)
    except asyncio.TimeoutError:
        # The drain itself overran — something is still holding the pipes open.
        out = err = ""
        for task in (out_task, err_task):
            task.cancel()
        await _kill(proc)
    except BaseException:
        # Any other way out — a cancellation from `stop --how hard`, or something
        # raised while pumping — must still take the child with it. Only the timeout
        # path killed the tree, so a harness could be left running and spending after
        # the call that owned it had gone.
        for task in (out_task, err_task):
            task.cancel()
        await _kill(proc)
        log.finish(None, time.monotonic() - started, "cancelled")
        raise
    finally:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed

    if timed_out:
        log.finish(None, time.monotonic() - started, f"timed out after {timeout}s")
        return Reply(
            ok=False,
            error=f"timed out after {timeout}s",
            duration=time.monotonic() - started,
        )

    duration = time.monotonic() - started
    if proc.returncode != 0:
        detail = (err or out).strip()
        log.finish(proc.returncode, duration, detail[-2000:])
        return Reply(
            ok=False,
            # Kept even on failure: a harness that reports its own errors in its own
            # output format can parse this and say what went wrong in one line,
            # instead of the caller showing two kilobytes of tail.
            text=out,
            error=f"exit code {proc.returncode}: {detail[-2000:]}" if detail
            else f"exit code {proc.returncode}",
            exit_code=proc.returncode,
            duration=duration,
            stderr=err,
        )
    log.finish(0, duration)
    return Reply(ok=True, text=out, exit_code=0, duration=duration, stderr=err)


async def _kill(proc) -> None:
    """End the harness and everything it started, and wait for it to be gone."""
    if proc.returncode is not None:
        return
    await _terminate_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:  # pragma: no cover - defensive
        pass


async def _feed_stdin(proc, stdin_data: str | None) -> None:
    """Write the prompt and close stdin; a harness waits for EOF before starting."""
    if proc.stdin is None:
        return
    try:
        if stdin_data is not None:
            proc.stdin.write(stdin_data.encode("utf-8"))
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # the harness exited before reading its prompt; the exit code explains it
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()


async def _pump(
    stream,
    on_line: Callable[[str], None] | None,
    call_log: CallLog | None = None,
    which: str = "out",
) -> str:
    """Read a pipe to EOF, handing complete lines to `on_line` as they arrive.

    Chunk-read rather than `readline()`: asyncio's StreamReader raises once a line
    exceeds its 64 KiB limit, and harness JSONL lines regularly do.

    The tee into `call_log` sits in `take`, not beside `on_line`: `on_line` skips blank
    lines and only ever sees stdout, and a console log that silently drops blank lines
    is not the thing that was printed.
    """
    if stream is None:
        return ""
    log = call_log or DISCARD
    # A bounded window over the *end* of the output. Every harness puts the event that
    # carries the answer, the token count and the session id last — `result`,
    # `turn.completed`, `step-finish` — so keeping the head of a flood discarded
    # precisely the part worth having, and left the parsers reading a fragment.
    collected: deque[str] = deque()
    collected_bytes = 0
    buffer = b""

    def take(raw: bytes) -> None:
        nonlocal collected_bytes
        text = _decode(raw)
        log.write(which, text)
        collected.append(text)
        collected_bytes += len(raw)
        while collected_bytes > MAX_OUTPUT_BYTES and len(collected) > 1:
            collected_bytes -= len(collected.popleft().encode("utf-8", "replace"))

    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                if len(buffer) > MAX_LINE_BYTES:  # pathological single line
                    line, buffer = buffer, b""
                else:
                    break
            else:
                line, buffer = buffer[:index], buffer[index + 1 :]
            take(line + b"\n")
            if on_line is not None:
                text = _decode(line).rstrip("\r")
                if text:
                    on_line(text)
    if buffer:
        take(buffer)
        if on_line is not None:
            text = _decode(buffer).rstrip("\r")
            if text:
                on_line(text)
    return "".join(collected)


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = raw[:MAX_OUTPUT_BYTES]
    return raw.decode("utf-8", errors="replace")


async def _terminate_tree(proc) -> None:
    """Kill the harness and every child it spawned.

    A hung harness rarely hangs alone — it has a model client, sometimes a language
    server, underneath it. Killing only the process we started leaves those running
    and still spending tokens, so the whole tree goes.
    """
    if WINDOWS:
        await _terminate_windows(proc)
    else:
        _terminate_posix(proc)


def _terminate_posix(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    _kill_directly(proc)


async def _terminate_windows(proc) -> None:
    """Windows has no process group to signal, so the tree is walked explicitly.

    `taskkill /T` follows the parent-child links and ships with every Windows;
    `os.killpg`, `os.getpgid` and `signal.SIGKILL` do not exist here at all.
    """
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(proc.pid),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=10)
    except (OSError, asyncio.TimeoutError):  # taskkill missing or itself wedged
        pass
    # Belt and braces: taskkill reports failure for a process that has already gone,
    # and cannot always reach one owned by another integrity level.
    _kill_directly(proc)


def _kill_directly(proc) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):  # already gone
        pass
