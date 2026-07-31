"""Uniform subprocess interface to every agent harness.

All panelists are CLI agents. The orchestrator only ever does: send a prompt, get
a final message back. Everything hard (tool loops, file reading, context handling)
lives inside the harness.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import signal
import time
from dataclasses import dataclass, field

# A harness that floods stdout is a bug, not a reason to exhaust memory.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

WINDOWS = os.name == "nt"

#: ERROR_FILENAME_EXCED_RANGE. Windows caps the whole command line at 32767 characters
#: and reports the overflow as this, which reaches Python as FileNotFoundError(errno=2).
_WIN_COMMAND_LINE_TOO_LONG = 206


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

    def __init__(self, model: str | None = None, variant: str | None = None, **kwargs):
        self.model = model
        self.variant = variant
        self.options = kwargs

    #: Whether this harness can continue a prior conversation by id.
    supports_sessions = False

    async def ask(
        self, prompt: str, cwd: str, timeout: int, session: str | None = None
    ) -> Reply:
        """Send a prompt. With `session`, continue that conversation instead of a new one."""
        raise NotImplementedError


def resolve_binary(name: str) -> str:
    """The name of a harness executable in the form the OS can actually start.

    Windows needs this. `CreateProcess` only ever appends `.exe`, so a bare "codex"
    never finds the `codex.cmd` that npm installs — every such panelist would be
    dropped as "not found" despite being installed and authenticated. `shutil.which`
    applies PATHEXT and returns the real path. An unresolvable name is handed back
    unchanged so the failure surfaces as the usual "executable not found".
    """
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name
    return shutil.which(name) or name


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
        return AdapterError(f"harness executable not found: {binary} ({exc})")
    return AdapterError(f"could not start {binary}: {exc}")


async def run_process(
    argv: list[str],
    cwd: str,
    timeout: int,
    stdin_data: str | None = None,
    env: dict | None = None,
) -> Reply:
    """Run a harness process to completion, killing it and its children on timeout.

    stdin is always either fed `stdin_data` and closed, or connected to /dev/null:
    a harness left with an open inherited stdin can block forever waiting for input.
    """
    started = time.monotonic()
    full_env = {**os.environ, **(env or {})}
    argv = [resolve_binary(argv[0]), *argv[1:]]

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
        raise _start_error(argv, exc) from exc

    payload = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=payload), timeout=timeout
        )
    except asyncio.TimeoutError:
        await _terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            pass
        return Reply(
            ok=False,
            error=f"timed out after {timeout}s",
            duration=time.monotonic() - started,
        )

    duration = time.monotonic() - started
    out = _decode(stdout)
    err = _decode(stderr)
    if proc.returncode != 0:
        detail = (err or out).strip()
        return Reply(
            ok=False,
            error=f"exit code {proc.returncode}: {detail[-2000:]}" if detail
            else f"exit code {proc.returncode}",
            exit_code=proc.returncode,
            duration=duration,
            stderr=err,
        )
    return Reply(
        ok=True, text=out, exit_code=0, duration=duration, stderr=err
    )


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
