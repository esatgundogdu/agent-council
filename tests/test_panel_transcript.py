from council.config import parse_config
from council.envelope import CONTINUE, READY, Envelope
from council.panel import build_panel, identity_map
from council.transcript import Transcript, Turn, estimate_tokens

FOUR = {
    "panel": [
        {"name": "gpt", "adapter": "mock"},
        {"name": "kimi", "adapter": "mock"},
        {"name": "glm", "adapter": "mock"},
        {"name": "deepseek", "adapter": "mock"},
    ]
}


def test_letters_are_assigned_in_order_and_unique():
    panel = build_panel(parse_config(dict(FOUR)), seed=1)
    assert [p.letter for p in panel] == ["A", "B", "C", "D"]
    assert {p.name for p in panel} == {"gpt", "kimi", "glm", "deepseek"}


def test_shuffle_hides_config_order():
    cfg = parse_config(dict(FOUR))
    seeds = {tuple(p.name for p in build_panel(cfg, seed=s)) for s in range(20)}
    # If the letter mapping were positional there would be exactly one ordering.
    assert len(seeds) > 1


def test_shuffle_is_reproducible_for_a_seed():
    cfg = parse_config(dict(FOUR))
    assert [p.name for p in build_panel(cfg, seed=7)] == [
        p.name for p in build_panel(cfg, seed=7)
    ]


def test_anonymize_false_keeps_config_order():
    cfg = parse_config(dict(FOUR))
    panel = build_panel(cfg, seed=3, anonymize=False)
    assert [p.name for p in panel] == ["gpt", "kimi", "glm", "deepseek"]


def test_identity_map_is_label_to_real_name():
    panel = build_panel(parse_config(dict(FOUR)), seed=2)
    mapping = identity_map(panel)
    assert set(mapping) == {"Agent-A", "Agent-B", "Agent-C", "Agent-D"}
    assert set(mapping.values()) == {"gpt", "kimi", "glm", "deepseek"}


def _turn(round_no, label, verdict=CONTINUE, comment="c"):
    return Turn.from_envelope(
        round_no, label, Envelope(comment=comment, verdict=verdict)
    )


def test_all_ready_requires_every_expected_panelist():
    t = Transcript()
    t.add(_turn(1, "Agent-A", READY))
    assert not t.all_ready(1, ["Agent-A", "Agent-B"])
    t.add(_turn(1, "Agent-B", READY))
    assert t.all_ready(1, ["Agent-A", "Agent-B"])


def test_single_continue_blocks_all_ready():
    t = Transcript()
    t.add(_turn(1, "Agent-A", READY))
    t.add(_turn(1, "Agent-B", CONTINUE))
    assert not t.all_ready(1, ["Agent-A", "Agent-B"])


def test_failed_turn_blocks_all_ready():
    t = Transcript()
    t.add(_turn(1, "Agent-A", READY))
    t.add(Turn.failure(1, "Agent-B", "timeout"))
    assert not t.all_ready(1, ["Agent-A", "Agent-B"])


def test_ready_in_an_earlier_round_does_not_carry_over():
    t = Transcript()
    t.add(_turn(1, "Agent-A", READY))
    t.add(_turn(1, "Agent-B", READY))
    t.add(_turn(2, "Agent-A", CONTINUE))
    t.add(_turn(2, "Agent-B", READY))
    assert not t.all_ready(2, ["Agent-A", "Agent-B"])


def test_plans_render_marks_the_readers_own_plan():
    from council.panel import Panelist

    t = Transcript()
    t.plans = {"Agent-A": "plan a", "Agent-B": "plan b"}
    reader = Panelist(name="x", letter="B", adapter="mock")
    rendered = t.render_plans(reader)
    assert "Agent-B's plan  ← THIS IS YOUR PLAN" in rendered
    assert "Agent-A's plan\n" in rendered


def test_conversation_is_empty_before_the_first_round():
    assert "first round" in Transcript().render_conversation()


def test_compaction_not_triggered_while_history_is_short():
    t = Transcript()
    for r in (1, 2):
        t.add(_turn(r, "Agent-A", comment="x" * 40000))
    # Only two rounds exist, and both must stay raw.
    assert not t.needs_compaction(100)


def test_compaction_triggers_once_history_grows():
    t = Transcript()
    for r in (1, 2, 3):
        t.add(_turn(r, "Agent-A", comment="x" * 4000))
    assert t.needs_compaction(100)
    assert t.rounds_to_compact() == [1]


def test_compaction_keeps_last_two_rounds_raw():
    t = Transcript()
    for r in range(1, 6):
        t.add(_turn(r, "Agent-A", comment=f"round {r} content"))
    t.apply_compaction("summary text", through_round=3)
    convo = t.render_conversation()
    assert "summary text" in convo
    assert "round 1 content" not in convo
    assert "round 4 content" in convo
    assert "round 5 content" in convo


def test_estimate_tokens_scales_with_length():
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 0


def test_transcript_md_groups_by_round():
    t = Transcript()
    t.add(_turn(1, "Agent-A", comment="first point"))
    t.add(_turn(2, "Agent-A", READY, comment="agreed"))
    md = t.render_transcript_md("header line")
    assert "header line" in md
    assert md.index("## Round 1") < md.index("## Round 2")
    assert "**Verdict: READY**" in md


def test_failed_turn_is_visible_in_transcript():
    t = Transcript()
    t.add(Turn.failure(1, "Agent-C", "timed out after 300s"))
    assert "timed out after 300s" in t.render_transcript_md()
