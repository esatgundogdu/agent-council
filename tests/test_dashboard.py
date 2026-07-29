"""The dashboard shows whatever build_state() returns, so that is what is tested."""

import json
import os

from council.dashboard import build_state, find_sessions, pid_alive, read_events


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


def test_read_events_on_a_missing_file(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == []
