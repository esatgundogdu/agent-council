"""Termination, failure policy and artefact tests driven by a stub adapter."""

import asyncio
import json
import os

import pytest

from council.adapters.base import Adapter, Delta, Reply
from council.config import parse_config
from council.orchestrator import Council, CouncilError, SessionPaths
from council.panel import build_panel

READY_MSG = '{"comment": "Agreed.", "verdict": "READY", "reason": "Settled."}'
CONTINUE_MSG = '{"comment": "Not yet.", "verdict": "CONTINUE", "reason": "Auth open."}'


class ScriptedAdapter(Adapter):
    """Returns replies from a per-panelist script; records what it was asked.

    `session_name` opts this stub into session continuity: it then reports a session
    id, so the orchestrator resumes it and sends deltas instead of full reassemblies.
    """

    supports_sessions = True

    def __init__(self, script=None, session_name=None, **kwargs):
        super().__init__(**kwargs)
        self.script = script or []
        self.calls = 0
        self.prompts = []
        self.sessions_seen = []
        self.session_name = session_name

    async def ask(self, prompt, cwd, timeout, session=None, on_delta=None):
        self.prompts.append(prompt)
        self.sessions_seen.append(session)
        i = self.calls
        self.calls += 1
        item = self.script[min(i, len(self.script) - 1)] if self.script else READY_MSG
        if isinstance(item, Reply):
            return item
        if on_delta is not None:
            on_delta(Delta(kind="text", text=item))
        return Reply(ok=True, text=item, session_id=self.session_name)


def make_council(
    tmp_path,
    scripts,
    task="Add a feature.",
    mode="independent",
    seed="",
    context="",
    **protocol,
):
    names = list(scripts)
    cfg = parse_config(
        {
            "panel": [{"name": n, "adapter": "mock"} for n in names],
            "protocol": {"min_rounds": 1, "max_rounds": 3, **protocol},
        }
    )
    panel = build_panel(cfg, seed=0, anonymize=False)
    paths = SessionPaths(root=tmp_path / ".council" / "s1")
    paths.prepare()
    paths.task.write_text(task, encoding="utf-8")
    if seed:
        paths.seed.write_text(seed, encoding="utf-8")
    if context:
        paths.context.write_text(context, encoding="utf-8")

    adapters = {}
    by_label = {}
    for p in panel:
        adapter = ScriptedAdapter(script=scripts[p.name])
        adapters[p.label] = adapter
        by_label[p.label] = adapter

    council = Council(
        config=cfg,
        panel=panel,
        paths=paths,
        project_dir=tmp_path,
        adapters=adapters,
        mode=mode,
    )
    return council, by_label


def run(council):
    return asyncio.run(council.run())


def read_events(paths):
    return [json.loads(l) for l in paths.events.read_text().splitlines() if l.strip()]


# ---- termination --------------------------------------------------------


def test_all_ready_ends_the_session(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    result = run(council)
    assert result.termination == "all_ready"
    assert result.rounds == 1


def test_min_rounds_blocks_a_first_round_consensus(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        min_rounds=2,
    )
    result = run(council)
    assert result.rounds == 2
    assert result.termination == "all_ready"
    kinds = [e["event"] for e in read_events(council.paths)]
    assert "early_ready_ignored" in kinds


def test_one_holdout_forces_another_round(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", CONTINUE_MSG, READY_MSG]},
    )
    result = run(council)
    assert result.rounds == 2
    assert result.termination == "all_ready"


def test_max_rounds_stops_an_endless_debate(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", CONTINUE_MSG]},
        max_rounds=2,
    )
    result = run(council)
    assert result.rounds == 2
    assert result.termination == "max_rounds"


def test_token_budget_cuts_the_session_short(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", CONTINUE_MSG]},
        max_rounds=5,
        token_budget=1,
    )
    result = run(council)
    assert result.termination == "token_budget"
    assert result.rounds == 1


def test_wall_clock_budget_cuts_the_session_short(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", CONTINUE_MSG]},
        max_rounds=5,
        wall_clock_budget=1,
    )
    council.started -= 10_000  # pretend the session has been running for ages
    result = run(council)
    assert result.termination == "wall_clock_budget"


def test_malformed_reply_never_ends_the_session(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", "I am done, looks good."], "b": ["plan b", READY_MSG]},
        max_rounds=2,
    )
    result = run(council)
    assert result.termination == "max_rounds"


# ---- failure handling ---------------------------------------------------


def test_each_plan_persists_as_its_call_finishes(tmp_path):
    """A crash after some panelists reply must keep their plans (no gather barrier)."""
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    panelist = council.panel[0]
    prompt = council._templates["independent_plan"]
    # Drive one panelist's Phase-1 coroutine in isolation and assert it committed
    # its plan before any sibling ran.
    label, reply = asyncio.run(council._plan_one(panelist, prompt, 60))
    assert reply.ok
    plan_file = council.paths.plans_dir / f"agent-{panelist.letter.lower()}.md"
    assert plan_file.is_file() and "plan a" in plan_file.read_text()
    kinds = [e["event"] for e in read_events(council.paths)]
    assert "plan_received" in kinds


def test_phase1_drop_order_is_panel_order_not_completion_order(tmp_path):
    """Two panelists fail; the dropped list must follow panel order deterministically."""
    council, adapters = make_council(
        tmp_path,
        {
            "a": ["plan a", READY_MSG],
            "b": [Reply(ok=False, error="b failed")],
            "c": [Reply(ok=False, error="c failed")],
            "d": ["plan d", READY_MSG],
        },
    )
    # Make the panel-order-later failure return FIRST, so completion order != panel order.
    slow, fast = adapters["Agent-B"], adapters["Agent-C"]
    slow_ask = slow.ask

    async def delayed(prompt, cwd, timeout, session=None, on_delta=None):
        await asyncio.sleep(0.05)
        return await slow_ask(prompt, cwd, timeout, session, on_delta)

    slow.ask = delayed
    run(council)
    assert [lbl for lbl, _ in council.dropped] == ["Agent-B", "Agent-C"]


def test_phase1_failure_drops_the_panelist(tmp_path):
    council, _ = make_council(
        tmp_path,
        {
            "a": [Reply(ok=False, error="boom")],
            "b": ["plan b", READY_MSG],
            "c": ["plan c", READY_MSG],
        },
    )
    result = run(council)
    assert len(result.panel) == 2
    assert result.dropped and result.dropped[0][1] == "boom"
    assert not (council.paths.plans_dir / "agent-a.md").exists()


def test_session_fails_when_panel_falls_below_two(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": [Reply(ok=False, error="boom")], "b": [Reply(ok=False, error="boom")]},
    )
    with pytest.raises(CouncilError, match="at least 2"):
        run(council)
    assert json.loads(council.paths.status.read_text())["state"] == "failed"


def test_one_failed_turn_is_noted_but_keeps_the_panelist(tmp_path):
    """`skip_with_note` means the panelist sits the round out, not the session.

    It used to drop on the first failure, so two unrelated transient timeouts on a
    three-seat panel lost quorum and ended the run — the outcome the policy exists to
    avoid, produced by the policy named after avoiding it.
    """
    council, _ = make_council(
        tmp_path,
        {
            "a": ["plan a", Reply(ok=False, error="timed out after 300s"), READY_MSG],
            "b": ["plan b", READY_MSG, READY_MSG],
            "c": ["plan c", READY_MSG, READY_MSG],
        },
        max_rounds=3,
    )
    result = run(council)
    assert "timed out after 300s" in council.paths.transcript.read_text()
    assert len(result.panel) == 3, "one failure must not remove a panelist"
    assert not result.dropped


def test_a_panelist_that_keeps_failing_is_eventually_dropped(tmp_path):
    from council.orchestrator import CONSECUTIVE_FAILURES_BEFORE_DROP

    dead = Reply(ok=False, error="timed out after 300s")
    council, _ = make_council(
        tmp_path,
        {
            "a": ["plan a"] + [dead] * (CONSECUTIVE_FAILURES_BEFORE_DROP + 1),
            "b": ["plan b"] + [CONTINUE_MSG] * CONSECUTIVE_FAILURES_BEFORE_DROP + [READY_MSG],
            "c": ["plan c"] + [CONTINUE_MSG] * CONSECUTIVE_FAILURES_BEFORE_DROP + [READY_MSG],
        },
        max_rounds=CONSECUTIVE_FAILURES_BEFORE_DROP + 1,
    )
    result = run(council)
    assert [label for label, _ in result.dropped] == ["Agent-A"]
    # …and its position is still in the digest, rather than quietly deleted.
    assert "Agent-A" in council.paths.digest.read_text(encoding="utf-8")


def test_abort_policy_stops_at_the_first_failure(tmp_path):
    council, _ = make_council(
        tmp_path,
        {
            "a": ["plan a", Reply(ok=False, error="nope")],
            "b": ["plan b", READY_MSG],
            "c": ["plan c", READY_MSG],
        },
    )
    council.config.on_failure = "abort"
    with pytest.raises(CouncilError, match="nope"):
        run(council)


# ---- prompts and artefacts ---------------------------------------------


def test_phase1_prompt_is_byte_identical_for_every_panelist(tmp_path):
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    run(council)
    first = [a.prompts[0] for a in adapters.values()]
    assert len(set(first)) == 1


def test_phase1_prompt_carries_the_task_verbatim(tmp_path):
    task = "Add OAuth.\n\n  - keep it simple\n\tliteral tab & unicode ✓\n"
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, task=task
    )
    run(council)
    assert task in list(adapters.values())[0].prompts[0]


def test_discussion_prompt_contains_plans_and_history(tmp_path):
    council, adapters = make_council(
        tmp_path,
        {"a": ["plan alpha", CONTINUE_MSG, READY_MSG], "b": ["plan beta", READY_MSG]},
        max_rounds=2,
    )
    run(council)
    second_round_prompt = adapters["Agent-A"].prompts[2]
    assert "plan alpha" in second_round_prompt
    assert "plan beta" in second_round_prompt
    assert "Not yet." in second_round_prompt  # round 1 is visible in round 2
    assert "THIS IS YOUR PLAN" in second_round_prompt


def test_later_speaker_sees_earlier_speaker_in_the_same_round(tmp_path):
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", READY_MSG]}, max_rounds=1
    )
    run(council)
    assert "Not yet." in adapters["Agent-B"].prompts[1]


# ---- durable event log --------------------------------------------------


def test_turn_events_preserve_the_full_argument_text(tmp_path):
    """A crash before Phase 3 must not lose the discussion: events.jsonl holds it."""
    council, _ = make_council(
        tmp_path, {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", READY_MSG]}, max_rounds=1
    )
    run(council)
    turns = [e for e in read_events(council.paths) if e["event"] == "turn_end"]
    assert turns, "no turn events recorded"
    for e in turns:
        assert "comment" in e and "reason" in e
    a = next(e for e in turns if e["agent"] == "Agent-A")
    assert a["comment"] == "Not yet."
    assert a["reason"] == "Auth open."


def test_the_lost_transcript_is_reconstructable_from_events_alone(tmp_path):
    """The exact gap this run hit: no transcript.md, yet the arguments survive."""
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", '{"comment": "distinct point ABC", "verdict": "CONTINUE"}'],
         "b": ["plan b", READY_MSG]},
        max_rounds=1,
    )
    run(council)
    council.paths.transcript.unlink()  # simulate the crash-before-Phase-3 case
    recovered = " ".join(
        e.get("comment", "")
        for e in read_events(council.paths)
        if e["event"] == "turn_end"
    )
    assert "distinct point ABC" in recovered


def test_turn_and_plan_events_record_token_cost(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    run(council)
    events = read_events(council.paths)
    for kind in ("plan_received", "turn_end"):
        sample = [e for e in events if e["event"] == kind]
        assert sample and all("tokens" in e for e in sample)
        # Mock replies carry no real usage, so the char/4 estimate must fill in.
        assert all(isinstance(e["tokens"], int) and e["tokens"] > 0 for e in sample)


def test_real_token_counts_are_preferred_over_the_estimate(tmp_path):
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    # Pretend Agent-A's harness reported real usage on its plan call.
    real = adapters["Agent-A"]
    orig = real.ask

    async def ask(prompt, cwd, timeout, session=None, on_delta=None):
        reply = await orig(prompt, cwd, timeout, session)
        reply.tokens = 9999
        return reply

    real.ask = ask
    run(council)
    plan = next(
        e for e in read_events(council.paths)
        if e["event"] == "plan_received" and e["agent"] == "Agent-A"
    )
    assert plan["tokens"] == 9999


def test_status_json_carries_pid_for_liveness_checks(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    run(council)
    status = json.loads(council.paths.status.read_text())
    assert status["pid"] == os.getpid()
    assert status["state"] == "done"


def test_status_json_records_pid_on_failure_too(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": [Reply(ok=False, error="boom")], "b": [Reply(ok=False, error="boom")]}
    )
    with pytest.raises(CouncilError):
        run(council)
    status = json.loads(council.paths.status.read_text())
    assert status["state"] == "failed"
    assert "pid" in status


def test_session_artefacts_are_written(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    run(council)
    p = council.paths
    assert p.transcript.is_file() and p.digest.is_file() and p.events.is_file()
    assert (p.plans_dir / "agent-a.md").is_file()
    assert (p.plans_dir / "agent-b.md").is_file()
    assert json.loads(p.status.read_text())["state"] == "done"


def test_events_record_the_real_identities(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    run(council)
    start = read_events(council.paths)[0]
    assert set(start["identities"].values()) == {"a", "b"}


def test_transcript_and_digest_never_name_the_models(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"gpt": ["plan a", READY_MSG], "deepseek": ["plan b", READY_MSG]},
    )
    run(council)
    for path in (council.paths.transcript, council.paths.digest):
        text = path.read_text().lower()
        assert "gpt" not in text and "deepseek" not in text


def test_digest_lists_open_points_when_panelists_disagree(tmp_path):
    council, _ = make_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", READY_MSG]},
        max_rounds=1,
    )
    run(council)
    digest = council.paths.digest.read_text()
    assert "Points still open" in digest
    assert "Auth open." in digest
    assert "may not be final" in digest


def test_digest_reports_consensus_when_all_agree(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )
    run(council)
    digest = council.paths.digest.read_text()
    assert "no panelist recorded an outstanding objection" in digest
    assert "Settled." in digest


# ---- session continuity -------------------------------------------------


def make_session_council(tmp_path, scripts, **protocol):
    """Council whose stubs report session ids, so the orchestrator resumes them."""
    council, adapters = make_council(tmp_path, scripts, **protocol)
    for label, adapter in adapters.items():
        adapter.session_name = f"sess-{label}"
    return council, adapters


def test_session_is_established_in_phase1_and_reused_afterwards(tmp_path):
    council, adapters = make_session_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    run(council)
    a = adapters["Agent-A"]
    # First call opens a session; every later call continues that same one.
    assert a.sessions_seen[0] is None
    assert a.sessions_seen[1:] == ["sess-Agent-A"] * (len(a.sessions_seen) - 1)
    assert council.sessions["Agent-A"] == "sess-Agent-A"


def test_continuation_prompt_omits_the_task_and_its_own_plan(tmp_path):
    council, adapters = make_session_council(
        tmp_path,
        {"a": ["MY OWN PLAN", READY_MSG], "b": ["plan beta", READY_MSG]},
        min_rounds=1,
        task="THE ORIGINAL TASK TEXT",
    )
    run(council)
    turn_prompt = adapters["Agent-A"].prompts[1]
    # Both are already in Agent-A's session; re-sending them wastes the whole point.
    assert "THE ORIGINAL TASK TEXT" not in turn_prompt
    assert "MY OWN PLAN" not in turn_prompt
    # What it has *not* seen must be there.
    assert "plan beta" in turn_prompt


def test_second_round_sends_only_what_is_new(tmp_path):
    council, adapters = make_session_council(
        tmp_path,
        {
            "a": ["plan a", '{"comment": "ROUND ONE FROM A", "verdict": "CONTINUE"}', READY_MSG],
            "b": ["plan b", '{"comment": "ROUND ONE FROM B", "verdict": "CONTINUE"}', READY_MSG],
        },
        min_rounds=2,
        max_rounds=2,
    )
    run(council)
    second = adapters["Agent-A"].prompts[2]  # A's round-2 turn
    assert "ROUND ONE FROM B" in second  # B spoke after A: new to A
    assert "ROUND ONE FROM A" not in second  # its own turn is already in its session
    assert "plan b" not in second  # plans were delivered in round 1


def test_later_speaker_still_sees_earlier_speaker_in_the_same_round(tmp_path):
    council, adapters = make_session_council(
        tmp_path,
        {
            "a": ["plan a", '{"comment": "A SPEAKS FIRST", "verdict": "CONTINUE"}'],
            "b": ["plan b", READY_MSG],
        },
        min_rounds=1,
        max_rounds=1,
    )
    run(council)
    assert "A SPEAKS FIRST" in adapters["Agent-B"].prompts[1]


def test_a_lost_session_falls_back_to_full_reassembly(tmp_path):
    council, adapters = make_session_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        min_rounds=1,
        task="THE ORIGINAL TASK TEXT",
    )
    a = adapters["Agent-A"]
    first_turn = {"done": False}
    orig = a.ask

    async def ask(prompt, cwd, timeout, session=None, on_delta=None):
        # Reject the resumed call once, as an expired session would.
        if session and not first_turn["done"]:
            first_turn["done"] = True
            a.prompts.append(prompt)
            return Reply(ok=False, error="session not found")
        return await orig(prompt, cwd, timeout, session)

    a.ask = ask
    result = run(council)

    assert result.rounds >= 1
    kinds = [e["event"] for e in read_events(council.paths)]
    assert "session_fallback" in kinds
    # The retry is a cold, complete prompt: task and plans are back in it.
    retry = a.prompts[-1]
    assert "THE ORIGINAL TASK TEXT" in retry and "plan b" in retry


def test_adapter_without_sessions_uses_the_stateless_path(tmp_path):
    council, adapters = make_council(  # no session_name: stub reports no session id
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]},
        min_rounds=1, task="THE ORIGINAL TASK TEXT",
    )
    run(council)
    assert council.sessions == {}
    assert "THE ORIGINAL TASK TEXT" in adapters["Agent-A"].prompts[1]


def test_session_continuity_can_be_switched_off(tmp_path):
    council, adapters = make_session_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    council.config.protocol.session_continuity = False
    run(council)
    assert all(s is None for s in adapters["Agent-A"].sessions_seen)


def test_compaction_never_runs_inside_a_panelists_session(tmp_path):
    """Summarising must not be injected into the debate context it summarises."""
    council, adapters = make_session_council(
        tmp_path,
        {"a": ["plan a", CONTINUE_MSG], "b": ["plan b", CONTINUE_MSG]},
        min_rounds=1,
        max_rounds=4,
        compaction_threshold=1,
    )
    run(council)
    kinds = [e["event"] for e in read_events(council.paths)]
    # Attempted, not necessarily accepted: this stub answers every call with its
    # scripted envelope, which the summary check now correctly refuses. What matters
    # here is which session the attempt used.
    assert "compaction_start" in kinds, "compaction did not trigger"
    compactor = adapters[council._compaction_panelist().label]
    assert None in compactor.sessions_seen[1:], "compaction reused a debate session"


def test_session_ids_are_recorded_for_future_resume(tmp_path):
    council, _ = make_session_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    run(council)
    events = read_events(council.paths)
    plans = [e for e in events if e["event"] == "plan_received"]
    assert plans and all(e.get("session") for e in plans)
    turns = [e for e in events if e["event"] == "turn_end"]
    assert turns and all(e.get("resumed") is True for e in turns)


def test_after_a_fallback_the_new_session_is_adopted(tmp_path):
    """The seam turn is cold, but the run must go warm again — not stay cold forever."""
    council, adapters = make_session_council(
        tmp_path,
        {
            "a": ["plan a", CONTINUE_MSG, CONTINUE_MSG, READY_MSG],
            "b": ["plan b", CONTINUE_MSG, CONTINUE_MSG, READY_MSG],
        },
        min_rounds=3,
        max_rounds=3,
    )
    a = adapters["Agent-A"]
    orig, rejected = a.ask, {"done": False}

    async def ask(prompt, cwd, timeout, session=None, on_delta=None):
        if session and not rejected["done"]:  # kill the first resumed call only
            rejected["done"] = True
            a.prompts.append(prompt)
            a.sessions_seen.append(session)
            return Reply(ok=False, error="session not found")
        return await orig(prompt, cwd, timeout, session)

    a.ask = ask
    run(council)

    # The retry itself is cold...
    assert a.sessions_seen[-3] is None or None in a.sessions_seen[2:]
    # ...but the session it opened was adopted, so a later turn resumes again.
    assert council.sessions["Agent-A"] == "sess-Agent-A"
    assert a.sessions_seen[-1] == "sess-Agent-A", "run stayed cold after the fallback"


def test_a_panelists_binary_reaches_the_adapter_that_runs_it(tmp_path):
    """The seam that makes `binary` worth having at all.

    council.yaml -> PanelistConfig -> Panelist -> adapter kwargs. Every hop is a place
    the field can be dropped silently, and a dropped one looks exactly like a harness
    that is not installed.
    """
    from council.adapters import build_adapter
    from council.config import CouncilConfig, PanelistConfig, ProtocolConfig
    from council.panel import build_panel

    config = CouncilConfig(
        panel=[
            PanelistConfig(name="gpt", adapter="codex_cli", binary="/opt/*/codex"),
            PanelistConfig(name="plain", adapter="codex_cli"),
        ],
        protocol=ProtocolConfig(anonymize=False),
    )
    panel = build_panel(config)
    assert {p.name: p.binary for p in panel} == {"gpt": "/opt/*/codex", "plain": None}

    for panelist in panel:
        kwargs = {"model": panelist.model, "variant": panelist.variant}
        if panelist.binary:
            kwargs["binary"] = panelist.binary
        adapter = build_adapter(panelist.adapter, **kwargs)
        # An unset binary must leave the adapter's own default alone, not blank it.
        expected = panelist.binary or "codex"
        assert adapter.binary == expected


# ---- failures the panel must survive, and lies it must not tell -----------


def test_a_bad_summary_is_refused_rather_than_replacing_the_discussion():
    """Accepting any non-blank reply erased the rounds it claimed to summarise.

    Compaction rewrites what every later prompt says about earlier rounds. A refusal,
    a stub, or the compactor answering as a panelist all used to qualify — and
    transcript.md renders every turn regardless, so nothing on disk showed the loss.
    """
    from council.orchestrator import _unusable_summary

    assert _unusable_summary("")
    assert _unusable_summary("I cannot summarise this.")
    assert _unusable_summary('{"comment": "' + "x" * 300 + '", "verdict": "CONTINUE"}')
    assert not _unusable_summary(
        "Rounds 1-2 settled the algorithm: a token bucket, chosen over a fixed window "
        "because it absorbs bursts. State lives on the handler class so tests can "
        "replace it. Open: whether Retry-After should round up, and where the limiter "
        "is constructed. Nobody objected to the 429 contract itself."
    )


def test_one_failed_turn_does_not_remove_a_panelist_for_the_session():
    """`skip_with_note` promised a panelist "sits the round out"; it dropped it.

    Two unrelated transient failures on a three-seat panel therefore ended the whole
    session, which is exactly what the policy exists to prevent.
    """
    from council.orchestrator import CONSECUTIVE_FAILURES_BEFORE_DROP

    assert CONSECUTIVE_FAILURES_BEFORE_DROP >= 2, (
        "a single timeout must not be evidence that a model is broken"
    )
