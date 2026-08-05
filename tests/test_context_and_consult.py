"""The context brief, and the `consult` mode that exists to be briefed.

The first test in this file is the one that matters. A brief is useful precisely
because it saves the panel from rediscovering what the convening agent already knows —
and that is the same reason it is dangerous: shown one turn too early, it replaces four
independent readings of a repository with four elaborations of one reading, and nothing
in the output would look any different. The boundary is only real while a test asserts
it.
"""

import asyncio
import json

import pytest

from council.adapters.base import Reply
from council.orchestrator import CouncilError

from test_orchestrator import (
    CONTINUE_MSG,
    READY_MSG,
    make_council,
    make_session_council,
    run,
)

BRIEF = "We already tried an in-process LRU and it thrashed under four workers."
TASK = "Should the cache move to Redis?"


def prompts_at(adapter, predicate):
    return [p for p in adapter.prompts if predicate(p)]


# ---- the rule -----------------------------------------------------------


def test_the_brief_never_reaches_a_panelist_writing_its_own_plan(tmp_path):
    """The load-bearing assertion of the whole feature.

    Phase 1 exists so that several models read this repository without inheriting
    anyone's framing. A brief in that prompt would end that quietly — the plans would
    still arrive, still look independent, and agree rather more than they used to.
    """
    council, adapters = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        task=TASK,
        context=BRIEF,
        min_rounds=1,
    )
    run(council)

    for label, adapter in adapters.items():
        phase1_prompt = adapter.prompts[0]
        assert TASK in phase1_prompt, f"{label} was not even given the task"
        assert BRIEF not in phase1_prompt, f"the brief leaked into {label}'s plan prompt"
    # And it did arrive — a rule that held because nothing was delivered proves nothing.
    assert any(BRIEF in p for p in adapters["Agent-A"].prompts[1:])


def test_hybrid_holds_the_brief_back_exactly_as_it_holds_the_proposal(tmp_path):
    council, adapters = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        mode="hybrid",
        seed="Use a token bucket.",
        context=BRIEF,
        min_rounds=1,
    )
    run(council)

    plan_prompt = adapters["Agent-A"].prompts[0]
    assert BRIEF not in plan_prompt
    assert "token bucket" not in plan_prompt
    first_turn = adapters["Agent-A"].prompts[1]
    assert BRIEF in first_turn and "token bucket" in first_turn


def test_review_gives_the_brief_immediately_because_there_is_no_plan_to_protect(tmp_path):
    council, adapters = make_council(
        tmp_path,
        {"a": [READY_MSG], "b": [READY_MSG]},
        mode="review",
        seed="Use a token bucket.",
        context=BRIEF,
        min_rounds=1,
    )
    run(council)

    assert BRIEF in adapters["Agent-A"].prompts[0]


def test_a_resumed_panelist_is_sent_the_brief_once(tmp_path):
    """It lives in the panelist's own conversation after that; re-sending is waste."""
    council, adapters = make_session_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG, READY_MSG], "b": ["plan b", CONTINUE_MSG, READY_MSG]},
        context=BRIEF,
        min_rounds=2,
        max_rounds=2,
    )
    run(council)

    carrying = prompts_at(adapters["Agent-A"], lambda p: BRIEF in p)
    assert len(carrying) == 1, "the brief was re-sent on a later round"


def test_a_council_with_no_brief_says_nothing_about_one(tmp_path):
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    run(council)

    assert "WHERE THIS WORK ALREADY STANDS" not in "".join(adapters["Agent-A"].prompts)
    assert "context brief" not in council.paths.digest.read_text(encoding="utf-8")


def test_the_digest_records_which_way_round_the_brief_arrived(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        context=BRIEF,
        min_rounds=1,
    )
    run(council)
    digest = council.paths.digest.read_text(encoding="utf-8")
    assert "independent plans before seeing it" in digest
    assert "`context.md`" in digest


def test_the_brief_is_logged_in_the_session_header(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        context=BRIEF,
        min_rounds=1,
    )
    run(council)
    created = json.loads(council.paths.events.read_text(encoding="utf-8").splitlines()[0])
    assert created["context_chars"] == len(BRIEF)


# ---- consult ------------------------------------------------------------


def test_consult_opens_in_parallel_and_then_debates_normally(tmp_path):
    council, adapters = make_council(
        tmp_path,
        {"a": [CONTINUE_MSG, READY_MSG], "b": [CONTINUE_MSG, READY_MSG]},
        mode="consult",
        task=TASK,
        context=BRIEF,
        min_rounds=2,
        max_rounds=4,
    )
    result = run(council)

    assert result.rounds == 2
    assert result.termination == "all_ready"
    assert not list(council.paths.plans_dir.glob("*.md"))

    opening = adapters["Agent-A"].prompts[0]
    assert TASK in opening and BRIEF in opening
    # The opening round asks for a reading of the situation, not for a plan.
    assert "wrote an independent plan" not in opening


def test_the_opening_round_reaches_everyone_in_the_next_one(tmp_path):
    """The bug this guards is invisible: a panel that simply never heard each other.

    The opening turns run concurrently, so none of them was in anyone's prompt. Marking
    them "seen" the way a round-robin turn is marked would drop every one of them from
    round 2, and the transcript would still read like a debate.

    It has to be a session council. Without one every later prompt is a full reassembly
    that carries the whole conversation regardless, so `_seen` is never consulted and
    the bug cannot show — which is exactly how the first version of this test passed
    against the broken code.
    """
    council, adapters = make_session_council(
        tmp_path,
        {
            "a": ['{"comment": "ALPHA SAYS CACHE", "verdict": "CONTINUE", "reason": "x"}',
                  READY_MSG],
            "b": ['{"comment": "BETA SAYS QUEUE", "verdict": "CONTINUE", "reason": "y"}',
                  READY_MSG],
        },
        mode="consult",
        min_rounds=2,
        max_rounds=2,
    )
    run(council)

    second_round_prompt = adapters["Agent-A"].prompts[1]
    assert "BETA SAYS QUEUE" in second_round_prompt, "Agent-A never heard Agent-B"
    assert "ALPHA SAYS CACHE" in adapters["Agent-B"].prompts[1]
    # Its own opening answer is already in its session; re-sending it is waste.
    assert "ALPHA SAYS CACHE" not in second_round_prompt


def test_the_opening_prompt_tells_the_truth_about_how_many_rounds_there_are(tmp_path):
    """Stale for a while, and found by a real panel reviewing this change.

    The template asserted flatly that there was no second round, from when `consult`
    always held exactly one. That is not a stale comment — it is a claim the model acts
    on, and a panelist told it has one shot when it has three hedges and empties its
    notebook into an answer it was going to get to revise.
    """
    many, adapters = make_council(
        tmp_path, {"a": [CONTINUE_MSG, READY_MSG], "b": [CONTINUE_MSG, READY_MSG]},
        mode="consult", min_rounds=2, max_rounds=3,
    )
    run(many)
    opening = adapters["Agent-A"].prompts[0]
    assert "no second round" not in opening
    assert "only chance" not in opening
    assert "will read your answer in the next round" in opening

    once, adapters = make_council(
        tmp_path / "solo", {"a": [READY_MSG], "b": [READY_MSG]},
        mode="consult", min_rounds=1, max_rounds=1,
    )
    run(once)
    opening = adapters["Agent-A"].prompts[0]
    assert "There is no second round" in opening
    assert "next round" not in opening


def test_one_round_is_just_a_round_limit(tmp_path):
    council, adapters = make_council(
        tmp_path, {"a": [CONTINUE_MSG], "b": [READY_MSG]},
        mode="consult", min_rounds=1, max_rounds=1,
    )
    result = run(council)

    assert result.rounds == 1
    assert [len(a.prompts) for a in adapters.values()] == [1, 1]
    digest = council.paths.digest.read_text(encoding="utf-8")
    assert "no panelist saw any other's answer" in digest
    assert "What each panelist said" in digest
    assert "Concerns raised" in digest
    assert "**Agent-A:** Auth open." in digest
    # No discussion happened, so the transcript is not one.
    assert "Full discussion" not in digest
    assert "Every answer in full" in digest
    assert "independent plans: `plans/`" not in digest


def test_a_debated_consult_drops_the_nobody_heard_anybody_warning(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": [CONTINUE_MSG, READY_MSG], "b": [CONTINUE_MSG, READY_MSG]},
        mode="consult", min_rounds=2, max_rounds=2,
    )
    run(council)
    digest = council.paths.digest.read_text(encoding="utf-8")

    assert "no panelist saw any other's answer" not in digest
    assert "the opening round was answered in parallel" in digest
    assert "Final position of each panelist" in digest
    assert "Full discussion" in digest


def test_opening_answers_are_recorded_in_panel_order_whoever_was_quickest(tmp_path):
    """Slow-first, so completion order is the reverse of panel order."""
    council, adapters = make_council(
        tmp_path, {"a": [READY_MSG], "b": [READY_MSG]},
        mode="consult", min_rounds=1, max_rounds=1,
    )
    original = adapters["Agent-A"].ask

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await original(*args, **kwargs)

    adapters["Agent-A"].ask = slow
    run(council)

    assert [t.label for t in council.transcript.turns] == ["Agent-A", "Agent-B"]


def test_one_failed_opening_turn_does_not_eliminate_a_panelist(tmp_path):
    """It sits the round out, like any other failure — there are rounds left to recover in."""
    council, _ = make_council(
        tmp_path,
        {
            "a": [Reply(ok=False, error="harness hiccup"), READY_MSG],
            "b": [CONTINUE_MSG, READY_MSG],
            "c": [CONTINUE_MSG, READY_MSG],
        },
        mode="consult", min_rounds=2, max_rounds=3,
    )
    run(council)

    assert [p.label for p in council.panel] == ["Agent-A", "Agent-B", "Agent-C"]
    assert council.paths.digest.read_text(encoding="utf-8").count("harness hiccup") == 0


def test_a_silent_panelist_still_leaves_a_digest_of_what_the_others_said(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": [Reply(ok=False, error="down")], "b": [READY_MSG]},
        mode="consult", min_rounds=1, max_rounds=1,
    )
    result = run(council)

    assert result.rounds == 1
    digest = council.paths.digest.read_text(encoding="utf-8")
    assert "Agent-B" in digest
    assert "Agent-A — no reply" in digest
