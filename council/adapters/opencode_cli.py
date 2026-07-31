"""Kimi / GLM / DeepSeek panelists via `opencode run` (Ollama Cloud).

Verified against opencode 1.18.4:

* The prompt is a positional argument — `opencode run` has no stdin prompt mode.
* stdin **must** be closed. With an inherited stdin the process hangs indefinitely
  instead of exiting (observed: a 30s task never returned). `run_process` connects
  stdin to /dev/null whenever no stdin payload is given, which covers this.
* `--format default` interleaves tool-result text with the assistant's reply and
  gives no separator between them, so the reply cannot be recovered. `--format json`
  emits JSONL events; only `part.type == "text"` parts are the assistant speaking.
* `step-finish` events carry real token counts, which beat our char/4 estimate.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .base import WINDOWS, Adapter, Reply, run_process

# ARG_MAX is 1 MiB on macOS and covers argv *and* the environment. Beyond this the
# prompt goes in via `-f`, whose contents opencode inlines into the message.
#
# Windows never puts the prompt on the command line at all: CreateProcess caps the
# whole line at 32767 characters, and an npm-installed `opencode` is a .cmd shim whose
# arguments cmd.exe re-parses, so a quote in a model's prose comes back mangled. The
# attachment path sidesteps both.
ARGV_PROMPT_LIMIT = 0 if WINDOWS else 200_000

ATTACHED_PROMPT_MESSAGE = (
    "Your instructions are in the attached file. Read it in full and respond to it "
    "exactly as if its contents had been sent to you as this message."
)


class OpencodeAdapter(Adapter):
    name = "opencode_cli"
    supports_sessions = True

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.binary = kwargs.get("binary", "opencode")
        self.agent = kwargs.get("agent", "council-plan")
        self.config_path = kwargs.get("config_path")
        #: Overridable so both delivery paths stay testable on either platform.
        self.argv_prompt_limit = kwargs.get("argv_prompt_limit", ARGV_PROMPT_LIMIT)

    async def ask(
        self, prompt: str, cwd: str, timeout: int, session: str | None = None
    ) -> Reply:
        base = [self.binary, "run", "--agent", self.agent, "--format", "json"]
        if self.model:
            base += ["-m", self.model]
        if self.variant:
            base += ["--variant", self.variant]
        if session:
            base += ["--session", session]
        base += ["--dir", cwd]

        env = {"OPENCODE_CONFIG": str(self.config_path)} if self.config_path else None

        with tempfile.TemporaryDirectory(prefix="council-opencode-") as tmp:
            argv = base + self._prompt_args(prompt, Path(tmp))
            # stdin_data stays None on purpose: run_process then wires stdin to
            # /dev/null, which is what stops opencode from hanging.
            reply = await run_process(argv, cwd=cwd, timeout=timeout, env=env)
        if not reply.ok:
            return reply

        text, tokens, session_id = parse_json_events(reply.text)
        if not text.strip():
            return Reply(
                ok=False,
                error="opencode returned no assistant text "
                f"(stderr: {reply.stderr.strip()[-500:] or 'empty'})",
                exit_code=reply.exit_code,
                duration=reply.duration,
                stderr=reply.stderr,
            )
        reply.text = text.strip()
        reply.tokens = tokens
        reply.session_id = session_id or session
        return reply

    def _prompt_args(self, prompt: str, tmpdir: Path) -> list[str]:
        """Prompt on the command line, or as an attachment when it is too big.

        opencode inlines an attached file into the message, so the model sees the
        same text either way. `-f` is variadic, hence the `--` before the message.
        """
        if len(prompt.encode("utf-8")) <= self.argv_prompt_limit:
            return [prompt]
        prompt_file = tmpdir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        return ["-f", str(prompt_file), "--", ATTACHED_PROMPT_MESSAGE]


def parse_json_events(stream: str) -> tuple[str, int | None, str | None]:
    """Extract assistant text, token usage and session id from an opencode JSONL stream.

    Text parts are keyed by id: a repeated id is a streaming update of the same part
    and must replace the earlier value rather than be appended twice.
    """
    parts: dict[str, str] = {}
    order: list[str] = []
    tokens: int | None = None
    session_id: str | None = None
    plain_lines: list[str] = []

    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            plain_lines.append(line)  # not JSON: keep in case the stream isn't JSONL
            continue
        if not isinstance(event, dict):
            continue
        if session_id is None:
            candidate = event.get("sessionID")
            if isinstance(candidate, str) and candidate:
                session_id = candidate
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text")
            if isinstance(text, str):
                key = str(part.get("id") or len(order))
                if key not in parts:
                    order.append(key)
                parts[key] = text
        elif ptype == "step-finish":
            usage = part.get("tokens")
            if isinstance(usage, dict) and isinstance(usage.get("total"), int):
                tokens = (tokens or 0) + usage["total"]

    if parts:
        return "\n\n".join(parts[k] for k in order).strip(), tokens, session_id
    # Not a JSONL stream after all (older opencode, or an error page): use it raw.
    return "\n".join(plain_lines).strip(), tokens, session_id
