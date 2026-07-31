import os
from pathlib import Path

from council.adapters.claude_cli import parse_claude_json
from council.adapters.codex_cli import clean_codex_error, parse_codex_usage
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
