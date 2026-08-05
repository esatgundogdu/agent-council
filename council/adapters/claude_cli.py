"""Claude panelist via `claude -p`.

Note it draws on the same subscription quota as the Claude Code session that started
the council.

`--output-format stream-json` emits one JSON object per line as the turn runs, so the
panelist can be watched live; the terminal `result` event still carries the final text
and real token usage, so this panelist is measured like the others rather than by a
char/4 estimate that would ignore its repo-exploration agent loop.
"""

from __future__ import annotations

import json

from .base import Adapter, Delta, LineParser, Reply, run_process

READ_ONLY_DENY = "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"


class ClaudeAdapter(Adapter):
    name = "claude_cli"

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.binary = kwargs.get("binary", "claude")

    def new_parser(self) -> LineParser:
        return ClaudeLineParser()

    async def ask(
        self,
        prompt: str,
        cwd: str,
        timeout: int,
        session: str | None = None,
        on_delta=None,
        call_log=None,
    ) -> Reply:
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "stream-json",
            # stream-json only emits the full event stream in print mode with --verbose;
            # --include-partial-messages adds the token-level text deltas.
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "plan",
            "--disallowedTools",
            READ_ONLY_DENY,
            "--add-dir",
            cwd,
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if session:
            argv += ["--resume", session]

        reply = await run_process(
            argv,
            cwd=cwd,
            timeout=timeout,
            stdin_data=prompt,
            on_line=self._line_sink(on_delta),
            call_log=call_log,
        )
        # Parsed even on a non-zero exit. `claude -p` exits 1 for an expired login or a
        # rate limit, but still emits a terminal `result` event that names the cause —
        # so reading it turns "exit code 1: <2 KB of JSONL tail>" into one sentence.
        text, tokens, error, session_id = parse_claude_stream(reply.text)
        if not reply.ok:
            reply.error = error or reply.error
            reply.session_id = session_id or session
            return reply
        if error:
            return Reply(
                ok=False,
                error=error,
                exit_code=reply.exit_code,
                duration=reply.duration,
                stderr=reply.stderr,
                session_id=session_id or session,
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
                session_id=reply.session_id,
            )
        return reply


class ClaudeLineParser(LineParser):
    """`stream-json` events -> deltas.

    Text comes from `stream_event` content-block deltas when partial messages are
    available. If none ever arrive (an older CLI, or the flag ignored), the complete
    `assistant` messages are used instead — the flag guards against emitting both.
    """

    def __init__(self) -> None:
        self.saw_partial = False

    def feed(self, line: str) -> list[Delta]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(event, dict):
            return []

        out: list[Delta] = []
        kind = event.get("type")
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id and kind == "system":
            out.append(Delta(kind="session", session_id=session_id))

        if kind == "system" and event.get("subtype") == "status":
            status = event.get("status")
            if isinstance(status, str):
                out.append(Delta(kind="status", text=status))
        elif kind == "stream_event":
            out += self._stream_event(event.get("event"))
        elif kind == "assistant":
            out += self._assistant(event.get("message"))
        elif kind == "result":
            usage = _claude_tokens(event.get("usage"))
            if usage:
                out.append(Delta(kind="usage", tokens=usage))
        return out

    def _stream_event(self, event: object) -> list[Delta]:
        if not isinstance(event, dict) or event.get("type") != "content_block_delta":
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return []
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        self.saw_partial = True
        return [Delta(kind="text", text=text)]

    def _assistant(self, message: object) -> list[Delta]:
        if not isinstance(message, dict):
            return []
        out: list[Delta] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                out.append(
                    Delta(
                        kind="tool",
                        tool=str(block.get("name") or "tool"),
                        target=_tool_target(block.get("input")),
                    )
                )
            elif block.get("type") == "text" and not self.saw_partial:
                text = block.get("text")
                if isinstance(text, str) and text:
                    out.append(Delta(kind="text", text=text))
        return out


def _tool_target(payload: object) -> str:
    """The most informative single argument of a tool call, for the activity line."""
    if not isinstance(payload, dict):
        return ""
    for key in ("file_path", "path", "pattern", "command", "query", "url", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def parse_claude_stream(
    stream: str,
) -> tuple[str, int | None, str | None, str | None]:
    """Extract (final text, token total, error, session id) from a stream-json run.

    The terminal `result` event is authoritative. A stream that is not JSONL at all
    (older CLI, crash page) degrades to the single-object `--output-format json`
    shape, and then to raw text, so a CLI change degrades rather than breaks.
    """
    result: dict | None = None
    session_id: str | None = None
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        candidate = event.get("session_id")
        if isinstance(candidate, str) and candidate:
            session_id = candidate
        if event.get("type") == "result":
            result = event

    if result is None:
        if session_id is not None:
            # It was streaming JSONL and then stopped without its terminal `result`:
            # truncated, or the CLI died. Falling through to the single-object parser
            # made `json.loads` fail on multi-line input, which returned the *raw
            # JSONL as the model's answer* — ok, no error — and that blob became the
            # panelist's argument in the transcript, the digest, and every other
            # panelist's next prompt.
            return (
                "",
                None,
                "claude stopped mid-stream: no final result event. The reply was "
                "truncated or the CLI exited early.",
                session_id,
            )
        text, tokens, error, sid = parse_claude_json(stream)
        return text, tokens, error, sid or session_id

    if result.get("is_error"):
        detail = result.get("result") or result.get("subtype") or "unknown error"
        return "", None, f"claude reported an error: {detail}", session_id

    text = result.get("result")
    if not isinstance(text, str):
        text = ""
    return text, _claude_tokens(result.get("usage")), None, session_id


def parse_claude_json(
    stream: str,
) -> tuple[str, int | None, str | None, str | None]:
    """The single-object `--output-format json` shape, kept as the degraded path."""
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
