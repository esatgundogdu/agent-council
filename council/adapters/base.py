"""Uniform subprocess interface to every agent harness.

All panelists are CLI agents. The orchestrator only ever does: send a prompt, get
a final message back. Everything hard (tool loops, file reading, context handling)
lives inside the harness.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field

# A harness that floods stdout is a bug, not a reason to exhaust memory.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


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


async def run_process(
    argv: list[str],
    cwd: str,
    timeout: int,
    stdin_data: str | None = None,
    env: dict | None = None,
) -> Reply:
    """Run a harness process to completion, killing its whole process group on timeout.

    stdin is always either fed `stdin_data` and closed, or connected to /dev/null:
    a harness left with an open inherited stdin can block forever waiting for input.
    """
    started = time.monotonic()
    full_env = {**os.environ, **(env or {})}

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
            start_new_session=True,  # own process group, so we can kill children too
        )
    except FileNotFoundError as exc:
        raise AdapterError(
            f"harness executable not found: {argv[0]} ({exc})"
        ) from exc
    except OSError as exc:
        if getattr(exc, "errno", None) == 7:  # E2BIG
            raise AdapterError(
                f"prompt too large for the {argv[0]} command line "
                f"({len(stdin_data or '')} bytes). Lower protocol.compaction_threshold."
            ) from exc
        raise AdapterError(f"could not start {argv[0]}: {exc}") from exc

    payload = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=payload), timeout=timeout
        )
    except asyncio.TimeoutError:
        _kill_group(proc)
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


def _kill_group(proc) -> None:
    """Kill the harness and every child it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
