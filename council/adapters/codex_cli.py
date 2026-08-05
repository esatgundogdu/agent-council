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

from .base import Adapter, Delta, LineParser, Reply, run_process


class CodexAdapter(Adapter):
    name = "codex_cli"

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.binary = kwargs.get("binary", "codex")

    def new_parser(self) -> LineParser:
        return CodexLineParser()

    async def ask(
        self,
        prompt: str,
        cwd: str,
        timeout: int,
        session: str | None = None,
        on_delta=None,
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
                argv,
                cwd=cwd,
                timeout=timeout,
                stdin_data=prompt,
                on_line=self._line_sink(on_delta),
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


#: Item types that are the model speaking, rather than the model working.
_CODEX_MESSAGE_ITEMS = {"agent_message", "assistant_message", "message"}

#: The most informative field of a work item, in preference order. Verified against
#: codex-cli 0.146.0: a `command_execution` carries `command`, an `mcp_tool_call`
#: carries `server`/`tool` plus an `arguments.title` the model wrote itself — which is
#: the one a human actually wants to read.
_CODEX_TARGETS = ("command", "path", "file", "query", "pattern", "tool", "server")


class CodexLineParser(LineParser):
    """`codex exec --json` events -> deltas.

    Written defensively: codex's event vocabulary has changed across releases, so
    anything unrecognised is ignored rather than guessed at. Losing a live activity
    line costs nothing; the final message comes from `-o`, never from here.
    """

    def __init__(self) -> None:
        self.seen_text: dict[str, int] = {}

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

        kind = str(event.get("type") or "")
        if kind == "thread.started":
            thread = event.get("thread_id")
            if isinstance(thread, str) and thread:
                return [Delta(kind="session", session_id=thread)]
            return []

        out: list[Delta] = []
        usage = event.get("usage")
        if isinstance(usage, dict):
            step = sum(
                value
                for key in ("input_tokens", "output_tokens", "reasoning_output_tokens")
                if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
            )
            if step:
                out.append(Delta(kind="usage", tokens=step))

        # Accumulated, not returned early. One event may carry both an item and a
        # usage block, and returning on the usage dropped the item — so the panelist
        # appeared to say nothing on precisely the event that completed its message.
        if kind.startswith("item."):
            out += self._item(event.get("item"), kind)
        return out

    def _item(self, item: object, kind: str) -> list[Delta]:
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("item_type") or item.get("type") or "")
        if item_type in _CODEX_MESSAGE_ITEMS:
            text = item.get("text") or item.get("message") or ""
            if not isinstance(text, str) or not text:
                return []
            # Codex re-sends the whole message on each update; emit only the growth.
            key = str(item.get("id") or item_type)
            already = self.seen_text.get(key, 0)
            if len(text) <= already:
                return []
            self.seen_text[key] = len(text)
            return [Delta(kind="text", text=text[already:])]

        if item_type == "reasoning":
            return [Delta(kind="status", text="reasoning")] if kind == "item.started" else []
        if kind != "item.started" or not item_type:
            return []
        return [Delta(kind="tool", tool=item_type, target=_codex_target(item))]


def _codex_target(item: dict) -> str:
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        title = arguments.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:200]
    for key in _CODEX_TARGETS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


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
        # A record that is not a dict, or a count that is a string, used to raise out
        # of the adapter and — since `_ask` catches only AdapterError — take the whole
        # session with it, leaving status.json still saying "running". A token count
        # is worth exactly one token count.
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        step = sum(
            value
            for key in ("input_tokens", "output_tokens", "reasoning_output_tokens")
            if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
        )
        total = (total or 0) + step
    return total
