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
