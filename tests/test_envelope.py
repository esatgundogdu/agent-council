import json

from council.envelope import CONTINUE, READY, parse_envelope


def test_fenced_json_block():
    env = parse_envelope(
        'Here is my view.\n\n```json\n{"comment": "Use a queue.", '
        '"verdict": "READY", "reason": "Settled."}\n```'
    )
    assert env.verdict == READY
    assert env.comment == "Use a queue."
    assert env.reason == "Settled."
    assert not env.malformed


def test_bare_json_with_prose_around_it():
    env = parse_envelope(
        'Thinking out loud first.\n{"comment": "Not yet.", "verdict": "CONTINUE", '
        '"reason": "Auth is unresolved."}\nThanks!'
    )
    assert env.verdict == CONTINUE
    assert env.comment == "Not yet."
    assert not env.malformed


def test_last_envelope_wins():
    env = parse_envelope(
        '```json\n{"comment": "first", "verdict": "CONTINUE"}\n```\n'
        'On reflection:\n```json\n{"comment": "second", "verdict": "READY"}\n```'
    )
    assert env.comment == "second"
    assert env.verdict == READY


def test_key_casing_and_padding_tolerated():
    env = parse_envelope('{"Comment": "x", " VERDICT ": "ready", "Reason": "done"}')
    assert env.verdict == READY
    assert env.comment == "x"
    assert env.reason == "done"


def test_verdict_with_surrounding_noise():
    env = parse_envelope('{"comment": "x", "verdict": "READY."}')
    assert env.verdict == READY


def test_nested_braces_in_comment():
    env = parse_envelope(
        '{"comment": "Call foo({a: 1}) then bar()", "verdict": "CONTINUE"}'
    )
    assert env.verdict == CONTINUE
    assert "foo({a: 1})" in env.comment


def test_malformed_json_falls_back_to_continue():
    text = "I think we should use a queue, but the JSON is broken: {oops"
    env = parse_envelope(text)
    assert env.verdict == CONTINUE
    assert env.malformed
    assert env.comment == text


def test_no_envelope_at_all_keeps_full_text():
    text = "Just some prose with no envelope whatsoever."
    env = parse_envelope(text)
    assert env.verdict == CONTINUE
    assert env.malformed
    assert env.comment == text


def test_empty_reply():
    env = parse_envelope("   ")
    assert env.verdict == CONTINUE
    assert env.malformed
    assert env.comment == ""


def test_prose_verdict_on_its_own_line_is_honoured():
    env = parse_envelope("The plan looks complete to me.\n\nVERDICT: READY")
    assert env.verdict == READY
    assert env.malformed  # still flagged: it was not a well-formed envelope


def test_word_ready_inside_an_argument_does_not_end_the_debate():
    env = parse_envelope(
        "The cache is not ready for production traffic, so we must keep discussing."
    )
    assert env.verdict == CONTINUE


def test_envelope_without_comment_keeps_the_whole_reply():
    text = 'Long reasoning here.\n```json\n{"verdict": "READY", "reason": "ok"}\n```'
    env = parse_envelope(text)
    assert env.verdict == READY
    assert "Long reasoning here." in env.comment


def test_json_that_is_not_an_envelope_is_ignored():
    text = 'Config example: {"retries": 3}\n\nStill thinking about failure modes.'
    env = parse_envelope(text)
    assert env.verdict == CONTINUE
    assert env.malformed
    assert env.comment == text


BACKSLASH_N = chr(92) + "n"


def test_a_newline_the_model_escaped_twice_becomes_a_newline():
    r"""One real model writes `\n` where it means a line break, on every turn.

    Observed from gpt-5.6-luna across a whole session while gpt-5.6-sol, on the same
    panel, wrote real newlines throughout. The JSON is valid either way, so nothing
    fails — the digest simply carries a literal `\n` in the middle of a sentence, in
    the one artefact that gets handed to another agent to implement.
    """
    envelope = parse_envelope(
        json.dumps(
            {
                "comment": "First point." + BACKSLASH_N * 2 + "Second point.",
                "verdict": "READY",
                "reason": "Line one." + BACKSLASH_N + "Line two.",
            }
        )
    )
    assert envelope.comment == "First point.\n\nSecond point."
    assert envelope.reason == "Line one.\nLine two."
    assert BACKSLASH_N not in envelope.comment


def test_a_comment_already_in_lines_is_left_exactly_as_written():
    r"""A field laid out in real lines is read literally, `\n` in its prose included."""
    text = "Escape a newline as " + BACKSLASH_N + " in JSON.\n\nThat is the rule."
    envelope = parse_envelope(
        json.dumps({"comment": text, "verdict": "CONTINUE", "reason": ""})
    )
    assert envelope.comment == text


# ---- a READY must never be invented ---------------------------------------
#
# The module's one safety promise: an unparseable reply is taken at face value as a
# CONTINUE, never as consent. Every string below broke it, and each is a shape a real
# model produces. A false READY ends the council and ships the plan.

NOT_CONSENT = [
    # A blocker whose first words happen to be "Ready or not".
    "I have three unresolved blockers:\n\n1. The migration drops rows.\n2. No rollback."
    "\n\nReady or not, this will lose customer data. We must keep discussing.",
    # A polite sign-off at the foot of a list of objections — the scan took the last
    # match, so this outvoted everything above it.
    "The migration is unsafe and there is no backfill test.\n\n"
    "Ready to discuss further next round.",
    "Some notes.\nReady-made components are a trap; we must keep debating.",
    "The migration is unsafe.\nready when you have a rollback plan, not before.",
    # A trailing comma — the commonest JSON error models make — invalidates an
    # explicit CONTINUE, dropping the reply onto the prose path.
    'I object.\n\n{"comment":"Auth is broken","verdict":"CONTINUE",}\n\n'
    "Ready to discuss further next round.",
    # Says both words on their own lines: discussing verdicts, not casting one.
    "The rule is:\n\nREADY\n\nor\n\nCONTINUE\n\nI am not sure which applies yet.",
]


def test_prose_that_merely_contains_ready_is_not_consent():
    for reply in NOT_CONSENT:
        envelope = parse_envelope(reply)
        assert envelope.verdict == CONTINUE, f"invented a READY from: {reply[:60]!r}"


def test_a_deliberate_prose_verdict_is_still_honoured():
    """The escape hatch has to keep working, or a whole panel argues forever."""
    for reply in (
        "I am satisfied with the plan.\n\nREADY",
        "Everything is resolved.\n\nVerdict: READY\n",
        "No objections left.\n\n**READY**",
        "Looks good.\n\n> READY",
    ):
        assert parse_envelope(reply).verdict == READY, reply


def test_a_negated_verdict_field_is_not_consent():
    """`"verdict": "NOT READY"` read as READY — and was not even flagged malformed."""
    for value in ("NOT READY", "not-ready", "NOT_READY", "READY?", "READY once fixed"):
        envelope = parse_envelope(
            json.dumps({"comment": "blocked on auth", "verdict": value})
        )
        assert envelope.verdict == CONTINUE, value
        assert envelope.malformed, f"{value!r} is not a verdict this can read"


def test_an_ordinary_verdict_field_still_parses_cleanly():
    for value in ("READY", "ready", " Ready. ", "**READY**"):
        envelope = parse_envelope(json.dumps({"comment": "done", "verdict": value}))
        assert envelope.verdict == READY and not envelope.malformed, value
    for value in ("CONTINUE", "continue", "CONTINUE_DISCUSSION"):
        envelope = parse_envelope(json.dumps({"comment": "no", "verdict": value}))
        assert envelope.verdict == CONTINUE and not envelope.malformed, value
