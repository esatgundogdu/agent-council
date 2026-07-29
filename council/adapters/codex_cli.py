"""GPT panelist via `codex exec` (ChatGPT subscription, read-only sandbox).

Verified against codex-cli 0.145.0:
- stdout carries session banners and activity, so the final message is taken from
  `--output-last-message` instead.
- `--json` turns stdout into a JSONL event stream (the `-o` file still gets the clean
  message). A single `turn.completed` event carries the whole run's real token usage,
  which beats a char/4 estimate that would ignore the repo-exploration agent loop.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .base import Adapter, Reply, run_process


class CodexAdapter(Adapter):
    name = "codex_cli"
    supports_sessions = True

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.binary = kwargs.get("binary", "codex")

    async def ask(
        self, prompt: str, cwd: str, timeout: int, session: str | None = None
    ) -> Reply:
        with tempfile.TemporaryDirectory(prefix="council-codex-") as tmp:
            last_message = Path(tmp) / "last_message.txt"
            # Options must precede the `resume` subcommand; codex rejects them after it.
            # `--ephemeral` is deliberately absent: the session has to survive to be
            # resumable, which is what keeps a panelist's exploration context alive.
            argv = [
                self.binary,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",  # target repo need not be a git repo
                "--color",
                "never",
                "--json",  # emit token usage; -o still holds the clean final message
                "-C",
                cwd,
                "-o",
                str(last_message),
            ]
            if self.model:
                argv += ["-m", self.model]
            if session:
                argv += ["resume", session, "-"]
            else:
                argv.append("-")  # read the prompt from stdin

            reply = await run_process(
                argv, cwd=cwd, timeout=timeout, stdin_data=prompt
            )
            if not reply.ok:
                # Under --json a failure dumps the whole event stream; surface the
                # actual message instead of an unreadable blob.
                reply.error = clean_codex_error(reply.error)
                return reply

            tokens = parse_codex_usage(reply.text)  # stdout is JSONL under --json
            thread = parse_codex_thread(reply.text) or session
            if last_message.is_file():
                text = last_message.read_text(encoding="utf-8", errors="replace")
            else:
                text = reply.text  # fall back to stdout, banners and all

        text = text.strip()
        if not text:
            return Reply(
                ok=False,
                error="codex produced an empty final message",
                exit_code=reply.exit_code,
                duration=reply.duration,
                stderr=reply.stderr,
            )
        reply.text = text
        reply.tokens = tokens
        reply.session_id = thread
        return reply


def clean_codex_error(raw: str) -> str:
    """Pull the human-readable reason out of a codex JSONL failure dump."""
    messages: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"message"' not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"error", "turn.failed", "item.completed"}:
            continue
        holder = event.get("error") if isinstance(event.get("error"), dict) else event
        message = holder.get("message") if isinstance(holder, dict) else None
        if not isinstance(message, str):
            item = event.get("item")
            message = item.get("message") if isinstance(item, dict) else None
        if isinstance(message, str) and message.strip():
            messages.append(_innermost_message(message.strip()))

    if not messages:
        return raw
    # Keep the last (most specific) message; the prefix carries the exit code.
    prefix = raw.split(":", 1)[0] if raw.startswith("exit code") else "codex failed"
    return f"{prefix}: {messages[-1]}"


def _innermost_message(text: str) -> str:
    """codex nests provider errors as JSON inside a message string; unwrap one level."""
    try:
        nested = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(nested, dict):
        inner = nested.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
        if isinstance(nested.get("message"), str):
            return nested["message"]
    return text


def parse_codex_thread(stream: str) -> str | None:
    """The session id codex reports as `thread.started`, for resuming later."""
    for line in stream.splitlines():
        line = line.strip()
        if not line or "thread" not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def parse_codex_usage(stream: str) -> int | None:
    """Sum real token usage from codex's `turn.completed` events.

    `input_tokens` already includes the cached portion, so cached/cache-write counts
    are not added again. Returns None if no usage was reported (older codex, or the
    stream wasn't JSONL) so the orchestrator falls back to its estimate.
    """
    total: int | None = None
    for line in stream.splitlines():
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        step = (
            usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("reasoning_output_tokens", 0)
        )
        total = (total or 0) + step
    return total
