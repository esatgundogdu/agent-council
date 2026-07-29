"""Optional Claude panelist via `claude -p`.

Note it draws on the same subscription quota as the Claude Code session that started
the council. `--output-format json` gives both the final text (`result`) and real
token usage (`usage`), so this panelist is measured like the others rather than by a
char/4 estimate that would ignore its repo-exploration agent loop.
"""

from __future__ import annotations

import json

from .base import Adapter, Reply, run_process

READ_ONLY_DENY = "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"


class ClaudeAdapter(Adapter):
    name = "claude_cli"
    supports_sessions = True

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.binary = kwargs.get("binary", "claude")

    async def ask(
        self, prompt: str, cwd: str, timeout: int, session: str | None = None
    ) -> Reply:
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--disallowedTools",
            READ_ONLY_DENY,
            "--add-dir",
            cwd,
        ]
        if self.model:
            argv += ["--model", self.model]
        if session:
            argv += ["--resume", session]

        reply = await run_process(argv, cwd=cwd, timeout=timeout, stdin_data=prompt)
        if not reply.ok:
            return reply

        text, tokens, error, session_id = parse_claude_json(reply.text)
        if error:
            return Reply(
                ok=False,
                error=error,
                exit_code=reply.exit_code,
                duration=reply.duration,
                stderr=reply.stderr,
            )
        reply.text = text.strip()
        reply.tokens = tokens
        reply.session_id = session_id or session
        if not reply.text:
            return Reply(
                ok=False,
                error="claude returned an empty response",
                exit_code=reply.exit_code,
                duration=reply.duration,
                stderr=reply.stderr,
            )
        return reply


def parse_claude_json(
    stream: str,
) -> tuple[str, int | None, str | None, str | None]:
    """Extract (final text, token total, error, session id) from `claude --output-format json`.

    Total tokens processed = uncached input + cache creation + cache reads + output.
    If the output isn't the expected JSON object, fall back to treating it as plain
    text with no usage, so a CLI change degrades rather than breaks.
    """
    stream = stream.strip()
    try:
        payload = json.loads(stream)
    except (json.JSONDecodeError, ValueError):
        return stream, None, None, None  # not JSON: use raw, estimate tokens upstream
    if not isinstance(payload, dict):
        return stream, None, None, None

    if payload.get("is_error"):
        detail = payload.get("result") or payload.get("subtype") or "unknown error"
        return "", None, f"claude reported an error: {detail}", None

    text = payload.get("result")
    if not isinstance(text, str):
        text = ""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = None
    return text, _claude_tokens(payload.get("usage")), None, session_id


def _claude_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    fields = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    total = sum(usage.get(f, 0) for f in fields if isinstance(usage.get(f), int))
    return total or None
