"""Every client renders whatever build_state() returns, so that is what is tested.

Includes the V1 event vocabulary (a bare `turn`, `session_start`): sessions recorded
before the daemon existed still have to open.
"""

import json
import os

from council.events import read_events
from council.state import build_state, find_sessions, pid_alive


def write_session(tmp_path, events, status=None, task="Do the thing.", digest=None):
    session = tmp_path / ".council" / "2026-07-25_120000"
    (session / "plans").mkdir(parents=True)
    (session / "task.md").write_text(task, encoding="utf-8")
    (session / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    if status is not None:
        (session / "status.json").write_text(json.dumps(status), encoding="utf-8")
    if digest is not None:
        (session / "digest.md").write_text(digest, encoding="utf-8")
    return session


START = {
    "event": "session_start",
    "identities": {"Agent-A": "glm", "Agent-B": "claude"},
    "panel": [
        {"label": "Agent-A", "name": "glm", "model": "ollama-cloud/glm-5.2"},
        {"label": "Agent-B", "name": "claude", "model": "opus"},
    ],
}


def turn(agent, round_no, verdict="CONTINUE", **kw):
    return {
        "event": "turn", "agent": agent, "round": round_no, "verdict": verdict,
        "comment": kw.get("comment", "an argument"), "reason": kw.get("reason", ""),
        "tokens": kw.get("tokens", 100), "seconds": 5, "malformed": kw.get("malformed", False),
        "resumed": kw.get("resumed", True),
    }


def test_panel_carries_identities_verdicts_and_tokens(tmp_path):
    session = write_session(
        tmp_path,
        [
            START,
            {"event": "plan_received", "agent": "Agent-A", "chars": 900, "seconds": 30, "tokens": 500},
            turn("Agent-A", 1, "READY", reason="settled", tokens=250),
        ],
    )
    state = build_state(session)
    a = next(p for p in state["panel"] if p["label"] == "Agent-A")
    assert a["name"] == "glm" and a["model"] == "ollama-cloud/glm-5.2"
    assert a["verdict"] == "READY" and a["reason"] == "settled"
    assert a["tokens"] == 750  # plan + turn
    assert a["has_plan"] and a["plan"]["chars"] == 900


def test_latest_verdict_wins_across_rounds(tmp_path):
    session = write_session(
        tmp_path, [START, turn("Agent-A", 1, "CONTINUE"), turn("Agent-A", 2, "READY")]
    )
    a = next(p for p in build_state(session)["panel"] if p["label"] == "Agent-A")
    assert a["verdict"] == "READY"


def test_rounds_are_ordered_and_grouped(tmp_path):
    session = write_session(
        tmp_path,
        [START, turn("Agent-B", 2), turn("Agent-A", 1), turn("Agent-B", 1)],
    )
    rounds = build_state(session)["rounds"]
    assert [r["round"] for r in rounds] == [1, 2]
    assert len(rounds[0]["turns"]) == 2


def test_dropped_panelist_is_flagged_with_its_reason(tmp_path):
    session = write_session(
        tmp_path,
        [START, {"event": "panelist_dropped", "agent": "Agent-A", "reason": "403 forbidden"}],
    )
    state = build_state(session)
    a = next(p for p in state["panel"] if p["label"] == "Agent-A")
    assert a["dropped"] and "403" in a["drop_reason"]
    assert any(h["kind"] == "dropped" for h in state["health"])


def test_failed_turn_appears_in_the_round_and_in_health(tmp_path):
    session = write_session(
        tmp_path,
        [START, {"event": "turn_failed", "agent": "Agent-B", "round": 1, "error": "timed out"}],
    )
    state = build_state(session)
    assert state["rounds"][0]["turns"][0]["failed"] is True
    assert any(h["kind"] == "turn_failed" for h in state["health"])


def test_session_fallback_is_surfaced(tmp_path):
    session = write_session(
        tmp_path,
        [START, {"event": "session_fallback", "agent": "Agent-A", "round": 2, "error": "gone"}],
    )
    assert any(h["kind"] == "fallback" for h in build_state(session)["health"])


def test_running_status_with_a_dead_pid_reads_as_interrupted(tmp_path):
    session = write_session(
        tmp_path, [START],
        status={"state": "running", "pid": 999_999_999, "phase": 2, "round": 1},
    )
    status = build_state(session)["status"]
    assert status["state"] == "interrupted"
    assert status["live"] is False


def test_running_status_with_a_live_pid_reads_as_live(tmp_path):
    from datetime import datetime, timezone

    session = write_session(
        tmp_path, [START],
        status={
            "state": "running", "pid": os.getpid(), "phase": 2, "round": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    status = build_state(session)["status"]
    assert status["state"] == "running" and status["live"] is True


def test_a_stale_heartbeat_is_not_live_even_with_a_live_pid(tmp_path):
    session = write_session(
        tmp_path, [START],
        status={
            "state": "running", "pid": os.getpid(),
            "updated_at": "2020-01-01T00:00:00+00:00",
        },
    )
    assert build_state(session)["status"]["state"] == "interrupted"


def test_finished_session_keeps_its_state_and_digest(tmp_path):
    session = write_session(
        tmp_path, [START, turn("Agent-A", 1, "READY")],
        status={"state": "done", "pid": 1, "rounds": 2, "termination": "all_ready"},
        digest="# Council digest\n\nagreed.",
    )
    state = build_state(session)
    assert state["status"]["state"] == "done"
    assert state["has_digest"] and "agreed" in state["digest"]


def test_a_half_written_last_line_is_skipped(tmp_path):
    """events.jsonl is read while it is being appended to; that must not break polling."""
    session = write_session(tmp_path, [START, turn("Agent-A", 1)])
    with (session / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"event": "turn", "agent": "Agent-B", "rou')
    state = build_state(session)
    assert len(state["rounds"][0]["turns"]) == 1  # the torn line is ignored


def test_missing_status_file_is_tolerated(tmp_path):
    session = write_session(tmp_path, [START])
    assert build_state(session)["status"]["state"] == "unknown"


def test_corrupt_status_file_is_tolerated(tmp_path):
    session = write_session(tmp_path, [START], status={"state": "running"})
    (session / "status.json").write_text("{not json", encoding="utf-8")
    assert build_state(session)["status"]["state"] == "unknown"


def test_task_text_is_passed_through_verbatim(tmp_path):
    task = "Line one.\n\n\tTabbed line ✓\n"
    session = write_session(tmp_path, [START], task=task)
    assert build_state(session)["session"]["task"] == task


def test_find_sessions_returns_newest_first(tmp_path):
    root = tmp_path / ".council"
    for name in ("2026-07-01_100000", "2026-07-25_120000", "2026-07-10_090000"):
        (root / name).mkdir(parents=True)
        (root / name / "task.md").write_text("t", encoding="utf-8")
    assert [p.name for p in find_sessions(tmp_path)][0] == "2026-07-25_120000"


def test_find_sessions_on_a_repo_with_no_council_dir(tmp_path):
    assert find_sessions(tmp_path) == []


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(999_999_999) is False
    assert pid_alive(None) is False


def test_read_events_on_a_session_that_has_written_nothing(tmp_path):
    assert read_events(tmp_path / "nowhere") == []


# ---- the current event vocabulary ---------------------------------------


CREATED = {
    "seq": 1, "event": "session_created", "id": "s1", "mode": "independent",
    "identities": {"Agent-A": "gpt", "Agent-B": "claude"},
    "panel": [
        {"label": "Agent-A", "name": "gpt", "model": None, "adapter": "codex_cli"},
        {"label": "Agent-B", "name": "claude", "model": "opus", "adapter": "claude_cli"},
    ],
}


def write_v3(tmp_path, semantic, verbose=()):
    session = tmp_path / ".council" / "2026-07-30_090000"
    (session / "plans").mkdir(parents=True)
    (session / "task.md").write_text("Do the thing.", encoding="utf-8")
    (session / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in semantic), encoding="utf-8"
    )
    if verbose:
        (session / "stream.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in verbose), encoding="utf-8"
        )
    return session


def test_a_turn_in_flight_shows_who_is_speaking_and_what_they_have_written(tmp_path):
    session = write_v3(
        tmp_path,
        [CREATED, {"seq": 2, "event": "turn_start", "agent": "Agent-A", "round": 1, "phase": 2}],
        verbose=[
            {"seq": 3, "event": "turn_delta", "agent": "Agent-A", "kind": "text", "text": "The auth "},
            {"seq": 4, "event": "turn_delta", "agent": "Agent-A", "kind": "text", "text": "module is wrong."},
            {"seq": 5, "event": "turn_delta", "agent": "Agent-A", "kind": "tool",
             "tool": "read", "target": "auth.py"},
        ],
    )
    state = build_state(session)
    turn = state["rounds"][0]["turns"][0]
    assert turn["streaming"] is True
    assert turn["text"] == "The auth module is wrong."

    speaker = next(p for p in state["panel"] if p["label"] == "Agent-A")
    assert speaker["speaking"] is True
    assert speaker["activity"]["state"] == "exploring"
    assert speaker["activity"]["target"] == "auth.py"


def test_a_finished_turn_stops_streaming_and_carries_its_verdict(tmp_path):
    session = write_v3(
        tmp_path,
        [
            CREATED,
            {"seq": 2, "event": "turn_start", "agent": "Agent-A", "round": 1, "phase": 2},
            {"seq": 4, "event": "turn_end", "agent": "Agent-A", "round": 1,
             "verdict": "READY", "comment": "Agreed.", "reason": "settled", "tokens": 120},
        ],
        verbose=[{"seq": 3, "event": "turn_delta", "agent": "Agent-A", "kind": "text", "text": "partial"}],
    )
    state = build_state(session)
    turn = state["rounds"][0]["turns"][0]
    assert turn["streaming"] is False and turn["verdict"] == "READY"
    assert turn["comment"] == "Agreed."

    speaker = next(p for p in state["panel"] if p["label"] == "Agent-A")
    assert speaker["speaking"] is False and speaker["activity"] is None


def test_a_delivered_plan_ends_the_phase_one_turn(tmp_path):
    """Phase 1 has no turn_end, so a panelist would otherwise 'speak' for ever."""
    session = write_v3(
        tmp_path,
        [
            CREATED,
            {"seq": 2, "event": "turn_start", "agent": "Agent-A", "round": 0, "phase": 1},
            {"seq": 3, "event": "plan_received", "agent": "Agent-A", "chars": 500, "tokens": 900},
        ],
    )
    speaker = next(p for p in build_state(session)["panel"] if p["label"] == "Agent-A")
    assert speaker["speaking"] is False and speaker["has_plan"] is True


def test_a_chair_message_is_a_turn_but_never_a_panelist(tmp_path):
    session = write_v3(
        tmp_path,
        [CREATED, {"seq": 2, "event": "chair_message", "round": 1, "text": "Stay in scope.", "by": "user"}],
    )
    state = build_state(session)
    turn = state["rounds"][0]["turns"][0]
    assert turn["chair"] is True and turn["comment"] == "Stay in scope."
    assert not any(p["label"] == "Chair" for p in state["panel"])


def test_heartbeats_beat_status_json_for_the_running_totals(tmp_path):
    session = write_v3(
        tmp_path,
        [CREATED],
        verbose=[{"seq": 2, "event": "heartbeat", "tokens": 4242, "elapsed": 61.5}],
    )
    (session / "status.json").write_text(
        json.dumps({"state": "running", "pid": os.getpid(), "tokens": 10, "elapsed": 1}),
        encoding="utf-8",
    )
    status = build_state(session)["status"]
    assert status["tokens"] == 4242 and status["elapsed"] == 61.5


def test_both_logs_are_merged_in_sequence_order(tmp_path):
    session = write_v3(
        tmp_path,
        [CREATED, {"seq": 3, "event": "turn_start", "agent": "Agent-A", "round": 1}],
        verbose=[{"seq": 2, "event": "heartbeat", "tokens": 1}],
    )
    assert [e["seq"] for e in read_events(session)] == [1, 2, 3]
    assert [e["seq"] for e in read_events(session, from_seq=2)] == [3]
    assert [e["seq"] for e in read_events(session, verbose=False)] == [1, 3]


def test_an_agent_thread_holds_the_prompts_we_sent_and_what_came_back(tmp_path):
    from council.state import build_agent_thread

    session = write_v3(
        tmp_path,
        [
            CREATED,
            {"seq": 3, "event": "turn_end", "agent": "Agent-A", "round": 1,
             "verdict": "CONTINUE", "comment": "Not yet.", "tokens": 40},
            {"seq": 5, "event": "turn_failed", "agent": "Agent-B", "round": 2, "error": "timed out"},
        ],
        verbose=[
            {"seq": 2, "event": "prompt", "agent": "Agent-A", "round": 1, "phase": 2,
             "text": "You are Agent-A…"},
            {"seq": 4, "event": "turn_delta", "agent": "Agent-A", "kind": "tool",
             "tool": "read", "target": "auth.py"},
        ],
    )
    thread = build_agent_thread(session, "Agent-A")
    assert thread["name"] == "gpt"
    assert [e["role"] for e in thread["entries"]] == ["sent", "reply", "tool"]
    assert thread["entries"][0]["text"] == "You are Agent-A…"
    assert thread["entries"][1]["verdict"] == "CONTINUE"
    # Another panelist's trouble stays in that panelist's thread.
    assert [e["role"] for e in build_agent_thread(session, "Agent-B")["entries"]] == ["problem"]


def test_an_accepted_extension_changes_the_settings_it_extended(tmp_path):
    """`extend` really raises the limit, so the settings screen must say the new one.

    The protocol is captured once, when the session announces itself. A council raised
    from five rounds to eight went on reporting five — and the one view whose whole job
    is to say how this council is configured was the one view that was wrong.
    """
    session = write_session(
        tmp_path,
        [
            {**START, "protocol": {"max_rounds": 5, "min_rounds": 2}},
            {"event": "budget_extended", "field": "max_rounds", "value": 8},
        ],
    )
    protocol = build_state(session)["session"]["protocol"]
    assert protocol["max_rounds"] == 8
    assert protocol["min_rounds"] == 2  # untouched fields survive


def test_an_extension_before_the_panel_is_announced_is_not_lost(tmp_path):
    session = write_session(
        tmp_path, [{"event": "budget_extended", "field": "token_budget", "value": 99}]
    )
    assert build_state(session)["session"]["protocol"] == {"token_budget": 99}


def test_a_restored_panelist_stops_reading_as_dropped(tmp_path):
    """Restore worked in the orchestrator and nowhere else.

    The panelist really did rejoin the panel and speak again, while every view went on
    showing it greyed out — still offering the restore that had already happened.
    """
    session = write_session(
        tmp_path,
        [
            START,
            {"event": "panelist_dropped", "agent": "Agent-A", "reason": "removed by the user"},
            {"event": "panelist_restored", "agent": "Agent-A"},
        ],
    )
    panel = build_state(session)["panel"]
    a = next(p for p in panel if p["label"] == "Agent-A")
    assert a["dropped"] is False
    assert a["drop_reason"] is None


def test_a_skipped_turn_leaves_a_trace(tmp_path):
    """A skipped turn writes no turn, so without this the button did nothing visible."""
    session = write_session(
        tmp_path, [START, {"event": "turn_skipped", "agent": "Agent-B", "round": 2}]
    )
    health = build_state(session)["health"]
    assert any(h["kind"] == "skipped" and h["agent"] == "Agent-B" for h in health)


# ---- one bad byte must not make a session unreadable ----------------------
#
# Every line below crashed `build_state` with a TypeError or ValueError, and the API
# has no handler around it — so a single corrupt record answered 500 from every
# endpoint that touched the session, permanently.

CORRUPT_LINES = [
    '{"seq": null, "event": "heartbeat"}',
    '{"seq": "1", "event": "heartbeat"}',
    '{"seq": 4, "event": "turn_end", "agent": "Agent-A", "round": "one"}',
    '{"seq": 4, "event": "turn_start", "agent": "Agent-A", "round": [1]}',
    '{"seq": 1, "event": "session_created", "panel": 5}',
    '{"seq": 2, "event": "turn_end", "agent": "Agent-A", "round": {"x": 1}}',
]


def test_a_corrupt_record_degrades_instead_of_raising(tmp_path):
    for i, bad in enumerate(CORRUPT_LINES):
        session = tmp_path / f"s{i}" / ".council" / "2026-07-25_120000"
        (session / "plans").mkdir(parents=True)
        (session / "task.md").write_text("Do it.", encoding="utf-8")
        (session / "events.jsonl").write_text(
            json.dumps(START | {"seq": 1}) + "\n" + bad + "\n", encoding="utf-8"
        )
        state = build_state(session)  # must not raise
        assert state["session"]["task"] == "Do it.", bad


def test_a_seqless_record_neither_jumps_the_queue_nor_collides(tmp_path):
    """A duplicate seq would be silently skipped by an SSE client resuming a stream."""
    session = write_session(
        tmp_path,
        [
            {"seq": 10, "event": "round_start", "round": 1},
            {"event": "turn_end", "agent": "Agent-A", "round": 1, "verdict": "READY"},
            {"seq": 11, "event": "round_start", "round": 2},
        ],
    )
    events = read_events(session)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), "events must stay in order"
    assert len(set(seqs)) == len(seqs), "a duplicate seq loses an event on resume"
    assert [e["event"] for e in events] == ["round_start", "turn_end", "round_start"]


# ---- what a running turn is doing --------------------------------------
#
# A tool call used to update one roster caption that the next call overwrote, so a
# panelist reading a repository for three minutes showed an empty turn body and a line
# that flickered. Measured against a real run: codex emits about one message per four
# commands and no reasoning items at all, so this trail is most of what there is to see
# while a turn is in flight. `addToTrail` in web/src/reduce.ts must fold identically.


def deltas(agent, *pairs):
    out = [{"event": "turn_start", "agent": agent, "round": 1, "phase": 2}]
    for kind, value in pairs:
        if kind == "tool":
            out.append({"event": "turn_delta", "agent": agent, "kind": "tool",
                        "tool": "read", "target": value})
        else:
            out.append({"event": "turn_delta", "agent": agent, "kind": kind, "text": value})
    return out


def panelist(tmp_path, events, label="Agent-A"):
    state = build_state(write_session(tmp_path, events))
    return next(p for p in state["panel"] if p["label"] == label)


def test_a_running_turn_records_what_it_touched(tmp_path):
    member = panelist(tmp_path, deltas("Agent-A", ("tool", "a.py"), ("tool", "b.py")))
    assert member["trail"] == ["read a.py", "read b.py"]
    assert member["speaking"] is True


def test_the_same_line_twice_running_is_not_progress(tmp_path):
    member = panelist(
        tmp_path, deltas("Agent-A", ("tool", "a.py"), ("tool", "a.py"), ("tool", "b.py"))
    )
    assert member["trail"] == ["read a.py", "read b.py"]


def test_status_lines_join_the_trail(tmp_path):
    member = panelist(tmp_path, deltas("Agent-A", ("status", "reasoning"), ("tool", "a.py")))
    assert member["trail"] == ["reasoning", "read a.py"]


def test_the_trail_is_capped(tmp_path):
    from council.state import TRAIL_LIMIT

    many = [("tool", f"f{i}.py") for i in range(TRAIL_LIMIT + 15)]
    member = panelist(tmp_path, deltas("Agent-A", *many))
    assert len(member["trail"]) == TRAIL_LIMIT
    assert member["trail"][-1] == f"read f{TRAIL_LIMIT + 14}.py"


def test_text_deltas_do_not_pollute_the_trail(tmp_path):
    member = panelist(tmp_path, deltas("Agent-A", ("text", "hello "), ("tool", "a.py")))
    assert member["trail"] == ["read a.py"]


def test_the_trail_works_during_phase_1_where_there_is_no_round(tmp_path):
    """The reason it lives on the panelist and not on the turn.

    A Phase 1 turn carries round 0 and never enters `rounds` at all, so a trail hung off
    the turn would be invisible for exactly the longest, quietest stretch of a session —
    four panelists reading a repository for minutes with nothing on screen.
    """
    events = [
        START,
        {"event": "turn_start", "agent": "Agent-A", "round": 0, "phase": 1},
        {"event": "turn_delta", "agent": "Agent-A", "kind": "tool",
         "tool": "read", "target": "src/main.cpp"},
        {"event": "turn_delta", "agent": "Agent-A", "kind": "tool",
         "tool": "grep", "target": "Olay_Yolu_c"},
    ]
    state = build_state(write_session(tmp_path, events))
    assert state["rounds"] == []  # nothing to hang a trail on
    member = next(p for p in state["panel"] if p["label"] == "Agent-A")
    assert member["trail"] == ["read src/main.cpp", "grep Olay_Yolu_c"]
