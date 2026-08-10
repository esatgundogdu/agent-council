import json
import os
from pathlib import Path

from council.adapters.claude_cli import parse_claude_json, parse_claude_stream
from council.adapters.codex_cli import (
    CodexLineParser,
    clean_codex_error,
    parse_codex_usage,
)
from council.adapters.opencode_cli import (
    ARGV_PROMPT_LIMIT,
    ATTACHED_PROMPT_MESSAGE,
    OpencodeAdapter,
    parse_json_events,
)

# A real (trimmed) opencode --format json stream, captured from opencode 1.18.4.
STREAM = """
{"type":"step_start","sessionID":"s1","part":{"id":"prt_1","type":"step-start"}}
{"type":"text","sessionID":"s1","part":{"id":"prt_2","type":"text","text":"plan-council-design.md"}}
{"type":"step_finish","sessionID":"s1","part":{"id":"prt_3","type":"step-finish","tokens":{"total":2820,"input":2500,"output":320}}}
"""


def test_parses_text_and_tokens():
    text, tokens, _ = parse_json_events(STREAM)
    assert text == "plan-council-design.md"
    assert tokens == 2820


def test_tool_events_are_excluded_from_the_reply():
    stream = (
        '{"type":"tool","part":{"id":"t1","type":"tool","state":{"output":"LEAKED"}}}\n'
        '{"type":"text","part":{"id":"p1","type":"text","text":"the answer"}}\n'
    )
    text, _, _ = parse_json_events(stream)
    assert text == "the answer"
    assert "LEAKED" not in text


def test_repeated_part_id_replaces_rather_than_duplicates():
    stream = (
        '{"type":"text","part":{"id":"p1","type":"text","text":"partial"}}\n'
        '{"type":"text","part":{"id":"p1","type":"text","text":"partial and complete"}}\n'
    )
    text, _, _ = parse_json_events(stream)
    assert text == "partial and complete"


def test_distinct_parts_are_concatenated_in_order():
    stream = (
        '{"type":"text","part":{"id":"p1","type":"text","text":"first"}}\n'
        '{"type":"text","part":{"id":"p2","type":"text","text":"second"}}\n'
    )
    text, _, _ = parse_json_events(stream)
    assert text == "first\n\nsecond"


def test_token_counts_accumulate_across_steps():
    stream = (
        '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":100}}}\n'
        '{"type":"text","part":{"id":"p1","type":"text","text":"x"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":250}}}\n'
    )
    _, tokens, _ = parse_json_events(stream)
    assert tokens == 350


def test_non_json_output_is_returned_verbatim():
    text, tokens, _ = parse_json_events("Error: something went wrong")
    assert text == "Error: something went wrong"
    assert tokens is None


def test_empty_stream():
    assert parse_json_events("") == ("", None, None)


# ---- prompt delivery ----------------------------------------------------

# The threshold is 0 on Windows, so both delivery paths are exercised against an
# explicit limit rather than the platform default, which is asserted separately.
LIMIT = 1000


def _adapter(limit: int = LIMIT) -> OpencodeAdapter:
    return OpencodeAdapter(argv_prompt_limit=limit)


def test_small_prompt_goes_on_the_command_line(tmp_path):
    args = _adapter()._prompt_args("short prompt", tmp_path)
    assert args == ["short prompt"]


def test_large_prompt_is_attached_as_a_file(tmp_path):
    prompt = "x" * (LIMIT + 1)
    args = _adapter()._prompt_args(prompt, tmp_path)

    assert args[0] == "-f"
    # `--` is mandatory: -f is variadic and would otherwise swallow the message.
    assert args[2] == "--"
    assert args[3] == ATTACHED_PROMPT_MESSAGE
    assert Path(args[1]).read_text(encoding="utf-8") == prompt


def test_the_limit_itself_still_uses_argv(tmp_path):
    assert _adapter()._prompt_args("x" * LIMIT, tmp_path) == ["x" * LIMIT]


def test_multibyte_prompts_are_measured_in_bytes(tmp_path):
    # 'é' is 2 bytes: a prompt under the limit in characters can exceed it in bytes.
    prompt = "é" * (LIMIT // 2 + 1)
    assert len(prompt) < LIMIT
    assert _adapter()._prompt_args(prompt, tmp_path)[0] == "-f"


def test_windows_always_attaches_the_prompt(tmp_path):
    # CreateProcess caps the command line at 32767 chars and an npm `opencode` is a
    # .cmd shim that re-parses its arguments, so argv is never used there.
    expected = 0 if os.name == "nt" else 200_000
    assert ARGV_PROMPT_LIMIT == expected

    args = OpencodeAdapter()._prompt_args("even a tiny prompt", tmp_path)
    assert (args[0] == "-f") is (os.name == "nt")


# ---- codex real token usage (captured from codex-cli 0.145.0) ------------

CODEX_STREAM = (
    '{"type":"thread.started","thread_id":"t1"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"type":"agent_message"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":32454,"cached_input_tokens":15104,'
    '"cache_write_input_tokens":0,"output_tokens":101,"reasoning_output_tokens":12}}\n'
)


def test_codex_usage_sums_input_output_reasoning():
    # cached_input_tokens is a subset of input_tokens and must NOT be double counted.
    assert parse_codex_usage(CODEX_STREAM) == 32454 + 101 + 12


def test_codex_usage_accumulates_across_turns():
    stream = (
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":200,"output_tokens":20}}\n'
    )
    assert parse_codex_usage(stream) == 330


def test_codex_usage_absent_returns_none():
    assert parse_codex_usage('{"type":"turn.started"}\n') is None
    assert parse_codex_usage("not json at all") is None


# ---- codex live deltas (captured verbatim from codex-cli 0.146.0) --------
#
# A real run, trimmed only in the length of the pasted command strings. Keeping the
# actual events is the point: the vocabulary has changed across codex releases, and a
# fixture invented from the docs would not have caught that the item's kind is under
# `type`, not `item_type`.

CODEX_REAL = [
    '{"type": "thread.started", "thread_id": "019fb886-a753-79a3-866c-7dac17fd8866"}',
    '{"type": "turn.started"}',
    '{"type": "item.started", "item": {"id": "item_0", "type": "command_execution",'
    ' "command": "powershell.exe -Command \'Get-Content README.md\'",'
    ' "aggregated_output": "", "exit_code": null, "status": "in_progress"}}',
    '{"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",'
    ' "command": "powershell.exe -Command \'Get-Content README.md\'",'
    ' "aggregated_output": "execution error", "exit_code": 1, "status": "completed"}}',
    '{"type": "item.started", "item": {"id": "item_2", "type": "mcp_tool_call",'
    ' "server": "node_repl", "tool": "js", "arguments": {"code": "…",'
    ' "title": "Read README heading"}, "result": null, "status": "in_progress"}}',
    '{"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call",'
    ' "server": "node_repl", "tool": "js", "arguments": {"code": "…",'
    ' "title": "Read README heading"}, "status": "completed"}}',
    '{"type": "item.completed", "item": {"id": "item_3", "type": "agent_message",'
    ' "text": "# Plan Council"}}',
    '{"type": "turn.completed", "usage": {"input_tokens": 92831,'
    ' "cached_input_tokens": 74496, "cache_write_input_tokens": 0,'
    ' "output_tokens": 528, "reasoning_output_tokens": 208}}',
]


def _codex_deltas(lines):
    parser = CodexLineParser()
    return [d for line in lines for d in parser.feed(line)]


def test_codex_stream_yields_session_tools_text_and_usage():
    deltas = _codex_deltas(CODEX_REAL)
    kinds = [d.kind for d in deltas]
    assert kinds == ["session", "tool", "tool", "text", "usage"]

    assert deltas[0].session_id == "019fb886-a753-79a3-866c-7dac17fd8866"
    assert deltas[3].text == "# Plan Council"
    assert deltas[4].tokens == 92831 + 528 + 208


def test_codex_tool_deltas_name_what_the_panelist_did():
    tools = [d for d in _codex_deltas(CODEX_REAL) if d.kind == "tool"]
    assert [t.tool for t in tools] == ["command_execution", "mcp_tool_call"]
    assert "Get-Content README.md" in tools[0].target
    # An MCP call's own title beats its server name: it is what a human can read.
    assert tools[1].target == "Read README heading"


def test_codex_emits_one_tool_delta_per_call_not_per_state_change():
    """`item.started` and `item.completed` describe one action, not two."""
    started = [line for line in CODEX_REAL if '"item.started"' in line]
    completed = [line for line in CODEX_REAL if '"item.completed"' in line]
    assert started and completed  # the fixture really does carry both
    assert len([d for d in _codex_deltas(CODEX_REAL) if d.kind == "tool"]) == len(started)


def test_codex_agent_message_is_not_replayed_when_it_is_resent():
    """Codex resends an item in full on update; only the growth is a delta."""
    parser = CodexLineParser()
    first = '{"type":"item.completed","item":{"id":"m","type":"agent_message","text":"Hello"}}'
    grown = '{"type":"item.completed","item":{"id":"m","type":"agent_message","text":"Hello there"}}'
    assert [d.text for d in parser.feed(first)] == ["Hello"]
    assert [d.text for d in parser.feed(grown)] == [" there"]
    assert parser.feed(grown) == []


def test_codex_line_parser_ignores_what_it_does_not_know():
    parser = CodexLineParser()
    assert parser.feed('{"type":"some.future.event","payload":{"a":1}}') == []
    assert parser.feed("not json") == []
    assert parser.feed("") == []


# ---- claude real token usage (captured from claude 2.1.217) --------------

CLAUDE_JSON = (
    '{"is_error": false, "result": "the answer text", '
    '"usage": {"input_tokens": 2, "cache_creation_input_tokens": 7662, '
    '"cache_read_input_tokens": 18680, "output_tokens": 4}}'
)


def test_claude_json_extracts_text_and_tokens():
    text, tokens, error, _ = parse_claude_json(CLAUDE_JSON)
    assert text == "the answer text"
    assert tokens == 2 + 7662 + 18680 + 4
    assert error is None


def test_claude_json_reports_errors():
    text, tokens, error, _ = parse_claude_json(
        '{"is_error": true, "result": "auth failed"}'
    )
    assert error and "auth failed" in error
    assert text == ""


def test_claude_non_json_degrades_to_plain_text():
    # If the CLI ever stops emitting JSON, use the output as the reply, estimate tokens.
    text, tokens, error, _ = parse_claude_json("just some plain text")
    assert text == "just some plain text"
    assert tokens is None
    assert error is None


def test_claude_missing_usage_returns_no_tokens():
    text, tokens, error, _ = parse_claude_json('{"is_error": false, "result": "hi"}')
    assert text == "hi"
    assert tokens is None


# A real `claude -p --output-format stream-json` failure, trimmed: the CLI exits 1 and
# still says exactly what went wrong in the terminal `result` event.
CLAUDE_AUTH_FAILURE = (
    '{"type":"system","subtype":"status","status":"requesting",'
    '"session_id":"f3ab6462-73d0-4598-8a33-f93a02a06603"}\n'
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text",'
    '"text":"Failed to authenticate: OAuth session expired and could not be '
    'refreshed"}]},"session_id":"f3ab6462-73d0-4598-8a33-f93a02a06603",'
    '"error":"authentication_failed"}\n'
    '{"type":"result","subtype":"success","is_error":true,"duration_ms":556,'
    '"result":"Failed to authenticate: OAuth session expired and could not be '
    'refreshed","session_id":"f3ab6462-73d0-4598-8a33-f93a02a06603"}\n'
)


def test_claude_failure_is_read_out_of_the_stream_not_the_exit_code():
    """A failed run says why in one sentence, not two kilobytes of JSONL tail.

    `claude -p` exits non-zero for an expired login, so `run_process` alone reports
    "exit code 1: <tail of stdout>" — which for a stream-json run is a fragment of the
    last event. The stream itself carries the reason, so it is parsed either way.
    """
    text, _tokens, error, session_id = parse_claude_stream(CLAUDE_AUTH_FAILURE)
    assert text == ""
    assert error and "OAuth session expired" in error
    assert "{" not in error  # no raw JSON leaked into the message
    assert session_id == "f3ab6462-73d0-4598-8a33-f93a02a06603"


# ---- codex failure messages ---------------------------------------------


def test_codex_error_is_unwrapped_from_the_json_dump():
    raw = (
        'exit code 1: {"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,'
        '\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
        '\\"message\\":\\"The \'gpt-oss:20b\' model is not supported when using Codex '
        'with a ChatGPT account.\\"}}"}'
    )
    cleaned = clean_codex_error(raw)
    assert cleaned == (
        "exit code 1: The 'gpt-oss:20b' model is not supported when using Codex "
        "with a ChatGPT account."
    )
    assert "thread.started" not in cleaned


def test_codex_error_without_json_is_left_alone():
    assert clean_codex_error("exit code 2: command not found") == (
        "exit code 2: command not found"
    )


# ---- a harness must not put words in a panelist's mouth --------------------


def test_a_codex_event_carrying_both_an_item_and_usage_keeps_its_text():
    """Returning on the usage block dropped the message it arrived with."""
    deltas = CodexLineParser().feed(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "m1", "item_type": "agent_message",
                         "text": "Here is my whole reply."},
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
    )
    kinds = {d.kind: d for d in deltas}
    assert "text" in kinds and kinds["text"].text == "Here is my whole reply."
    assert kinds["usage"].tokens == 15


def test_codex_usage_counts_only_completed_turns():
    """It summed every event that mentioned usage, over-counting several times over.

    An inflated count spends `token_budget` early and ends the council on a limit it
    never actually reached.
    """
    stream = "\n".join(
        [
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}',
            '{"type":"item.completed","usage":{"input_tokens":100,"output_tokens":20}}',
            '{"type":"session.summary","usage":{"input_tokens":100,"output_tokens":20}}',
        ]
    )
    assert parse_codex_usage(stream) == 120


def test_a_codex_usage_field_of_the_wrong_type_costs_only_the_count():
    """It raised out of the adapter and took the whole session with it."""
    stream = '{"type":"turn.completed","usage":{"input_tokens":"12","output_tokens":3}}'
    assert parse_codex_usage(stream) == 3
    assert parse_codex_usage('["usage", 1]') is None


def test_opencode_does_not_return_a_warning_banner_as_the_reply():
    """A turn with no model output came back as a successful reply saying this."""
    text, _tokens, session = parse_json_events(
        "opencode: warning, model fell back to a cheaper tier\n"
        '{"sessionID":"s","part":{"type":"tool","id":"t1","tool":"read"}}'
    )
    assert text == "", "a warning line is not a panelist's argument"
    assert session == "s"


def test_a_truncated_claude_stream_is_an_error_not_an_answer():
    """The raw JSONL used to become the panelist's argument, ok and unflagged."""
    raw = (
        '{"type":"assistant","session_id":"s1",'
        '"message":{"content":[{"type":"text","text":"x"}]}}\n'
        '{"type":"assis'
    )
    text, _tokens, error, session = parse_claude_stream(raw)
    assert text == "" and error and "mid-stream" in error
    assert session == "s1"


# ── the claude panelist is read-only, and the argv is the whole of how ─────────


def _claude_argv(monkeypatch, **kwargs) -> list[str]:
    """The command line the claude adapter would actually run."""
    import asyncio

    from council.adapters import claude_cli

    seen: dict = {}

    async def capture(argv, **rest):
        seen["argv"] = argv
        return claude_cli.Reply(ok=True, text="{}")

    monkeypatch.setattr(claude_cli, "run_process", capture)
    adapter = claude_cli.ClaudeAdapter(**kwargs)
    asyncio.run(adapter.ask("prompt", cwd=".", timeout=60))
    return seen["argv"]


def test_a_panelist_cannot_touch_the_repository(monkeypatch):
    """Every tool that could change a file, named on the command line.

    A council reads; the main agent writes. That boundary is not enforced by asking
    the model nicely — the prompt does say so, but a prompt is a preference. This list
    is the enforcement, and in `-p` a denied tool is a refusal rather than a prompt
    somebody could wave through.
    """
    argv = _claude_argv(monkeypatch)
    denied = set(argv[argv.index("--disallowedTools") + 1].split(","))
    assert {"Edit", "Write", "NotebookEdit", "Bash"} <= denied


def test_the_panelist_is_not_put_in_plan_mode(monkeypatch):
    """Plan mode waits for an approval that headless mode can never give.

    Its contract is "propose the change and call `ExitPlanMode`", and under `-p` there
    is nobody on the other end. The panelist went looking for that tool and opened its
    plan by explaining that it could not use it — two of four plans in a real council
    began that way. Read-only was never what this flag was buying; the deny list is.
    """
    argv = _claude_argv(monkeypatch)
    assert "--permission-mode" not in argv
    assert "plan" not in argv


def test_a_resumed_turn_is_still_read_only(monkeypatch):
    """The second call is the one that would quietly lose the guarantee."""
    import asyncio

    from council.adapters import claude_cli

    seen: dict = {}

    async def capture(argv, **rest):
        seen["argv"] = argv
        return claude_cli.Reply(ok=True, text="{}")

    monkeypatch.setattr(claude_cli, "run_process", capture)
    adapter = claude_cli.ClaudeAdapter()
    asyncio.run(adapter.ask("prompt", cwd=".", timeout=60, session="abc123"))

    argv = seen["argv"]
    assert "--resume" in argv and "--disallowedTools" in argv
    assert "--permission-mode" not in argv
