"""Console capture: the raw output of every harness process.

Everything else about a turn has been through a parser by the time it is stored, so
these tests care about one property above all — that what a harness actually printed
survives, including the cases where nothing else does (a timeout, a non-zero exit).
"""

import asyncio
import sys

import pytest

from council.adapters.base import run_process
from council.calls import (
    DISCARD,
    STDERR_PREFIX,
    CallLog,
    call_filename,
)
from council.state import build_state
from test_orchestrator import READY_MSG, make_council, make_session_council, run


def read(log: CallLog) -> str:
    return log.path.read_text(encoding="utf-8")


# ---- the file itself ----------------------------------------------------


def test_header_names_the_command_that_was_actually_run(tmp_path):
    log = CallLog(
        tmp_path / "calls" / "0001-agent-a-r0.log",
        agent="Agent-A",
        phase=1,
        round_no=0,
        model="gpt-5.2-codex",
        effort="high",
        session="thread-42",
    )
    log.start([r"C:\tools\codex.cmd", "exec", "--sandbox", "read-only"], cwd="C:/repo")
    log.finish(0, 3.5)

    text = read(log)
    assert r"# command  : C:\tools\codex.cmd exec --sandbox read-only" in text
    assert "Agent-A" in text and "gpt-5.2-codex" in text and "high" in text
    assert "thread-42" in text  # a resumed call says which conversation it continued
    assert "C:/repo" in text
    assert "exit code: 0" in text and "3.5s" in text


def test_only_the_arguments_that_need_quoting_get_it(tmp_path):
    # So the line can be pasted back into a shell, and so the common case stays
    # readable rather than drowning in quotation marks.
    log = CallLog(tmp_path / "c.log")
    log.start([r"C:\Program Files\codex\codex.exe", "exec", "-C", r"C:\repo"], cwd=".")
    log.finish(0, 1.0)
    assert r'"C:\Program Files\codex\codex.exe" exec -C C:\repo' in read(log)


def test_a_prompt_passed_on_the_command_line_is_clipped(tmp_path):
    # opencode takes its prompt positionally on some platforms. It is already in
    # `stream.jsonl` in full, once; a second copy in every header is waste.
    log = CallLog(tmp_path / "c.log")
    log.start(["opencode", "run", "x" * 5_000], cwd=".")
    log.finish(0, 1.0)
    text = read(log)
    assert "(+4600 chars)" in text
    assert "x" * 5_000 not in text


def test_a_cold_call_says_so_rather_than_leaving_it_blank(tmp_path):
    log = CallLog(tmp_path / "c.log", agent="Agent-B")
    log.start(["claude", "-p"], cwd=".")
    log.finish(0, 1.0)
    assert "cold start" in read(log)


def test_stderr_is_marked_and_stdout_is_left_verbatim(tmp_path):
    log = CallLog(tmp_path / "c.log")
    log.start(["x"], cwd=".")
    log.write("out", '{"type":"result","ok":true}')
    log.write("err", "warning: model is deprecated")
    log.finish(0, 0.1)

    text = read(log)
    # stdout unprefixed, so a JSONL stream stays re-parsable by whoever wants to.
    assert '\n{"type":"result","ok":true}\n' in text
    assert f"{STDERR_PREFIX}warning: model is deprecated" in text
    assert "stdout   : 27 bytes" in text and "stderr   : 28 bytes" in text


def test_a_flood_keeps_both_ends_and_says_what_it_dropped(tmp_path):
    log = CallLog(tmp_path / "c.log", limit=4_000, tail_bytes=400)
    log.start(["x"], cwd=".")
    log.write("out", "FIRST LINE")
    for i in range(500):
        log.write("out", f"noise {i:04d} " + "x" * 60)
    log.write("out", "LAST LINE")
    log.finish(0, 1.0)

    text = read(log)
    # The head has the command and what went wrong first; the tail has whatever it was
    # doing when it died. Dropping either end loses a different half of the diagnosis.
    assert "FIRST LINE" in text
    assert "LAST LINE" in text
    assert "noise 0250" not in text  # the middle really is gone
    assert "bytes omitted here" in text
    assert log.truncated and log.skipped > 0
    assert "omitted  :" in text  # and the footer admits it


def test_the_full_size_is_reported_even_when_the_file_is_capped(tmp_path):
    log = CallLog(tmp_path / "c.log", limit=2_000, tail_bytes=200)
    log.start(["x"], cwd=".")
    for i in range(200):
        log.write("out", "y" * 80)
    log.finish(0, 1.0)
    # `bytes` is what the harness printed, not what fitted — the number the UI shows.
    assert log.bytes_seen == 200 * 80
    assert log.written < log.bytes_seen


def test_an_unwritable_path_costs_the_log_and_nothing_else(tmp_path):
    # The directory is a file, so `mkdir` fails. A council must not die of this.
    (tmp_path / "blocked").write_text("not a directory", encoding="utf-8")
    log = CallLog(tmp_path / "blocked" / "calls" / "c.log")
    log.start(["x"], cwd=".")
    log.write("out", "something")
    log.finish(0, 1.0)
    assert not log.path.exists()
    assert log.out_bytes == 9  # still counted, so the event is still truthful


def test_discard_records_nothing_and_never_touches_disk(tmp_path):
    DISCARD.start(["x"], cwd=".")
    DISCARD.write("out", "hello")
    DISCARD.finish(0, 1.0)
    assert not (tmp_path / ".").joinpath("c.log").exists()
    assert DISCARD.written == 0


def test_names_are_unique_per_call_not_per_turn():
    # The cold-fallback path calls the same panelist twice for one round. Naming both
    # after the round would leave only the retry, losing the failure that caused it.
    assert call_filename(7, "Agent-B", 2) != call_filename(8, "Agent-B", 2)
    assert call_filename(7, "Agent-B", 2) == "0007-agent-b-r2.log"


# ---- the tee through a real process -------------------------------------


def test_a_real_process_has_both_its_streams_captured(tmp_path):
    log = CallLog(tmp_path / "c.log")
    script = (
        "import sys; "
        "print('on stdout'); "
        "print('on stderr', file=sys.stderr); "
        "sys.exit(3)"
    )
    reply = asyncio.run(
        run_process([sys.executable, "-c", script], cwd=str(tmp_path), timeout=30,
                    call_log=log)
    )
    assert reply.exit_code == 3
    text = read(log)
    assert "on stdout" in text
    assert f"{STDERR_PREFIX}on stderr" in text
    assert "exit code: 3" in text


def test_a_timed_out_call_still_leaves_a_footer(tmp_path):
    # The one call whose log is the *only* record: no turn_end, no envelope, no text.
    log = CallLog(tmp_path / "c.log")
    script = "import sys, time; print('started', flush=True); time.sleep(30)"
    reply = asyncio.run(
        run_process([sys.executable, "-c", script], cwd=str(tmp_path), timeout=2,
                    call_log=log)
    )
    assert not reply.ok and "timed out" in reply.error
    text = read(log)
    assert "started" in text
    assert "exit code: killed" in text and "timed out after 2s" in text


def test_blank_lines_are_kept(tmp_path):
    # The delta sink skips them; a console log that did would not be the thing printed.
    log = CallLog(tmp_path / "c.log")
    script = "print('a'); print(); print('b')"
    asyncio.run(
        run_process([sys.executable, "-c", script], cwd=str(tmp_path), timeout=30,
                    call_log=log)
    )
    body = read(log).split("─" * 76 + "\n", 1)[1]
    assert body.startswith("a\n\nb\n")


# ---- wired into a council -----------------------------------------------


def test_every_call_leaves_a_log_the_snapshot_can_find(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    run(council)

    state = build_state(council.paths.root)
    by_label = {p["label"]: p for p in state["panel"]}
    for label in ("Agent-A", "Agent-B"):
        calls = by_label[label]["calls"]
        # Phase 1 plus one discussion round.
        assert [c["round"] for c in calls] == [0, 1]
        assert [c["phase"] for c in calls] == [1, 2]
        for call in calls:
            assert (council.paths.calls_dir / call["file"]).is_file()
            assert call["ok"] and not call["truncated"]


def test_phase_one_is_covered_even_though_it_has_no_round(tmp_path):
    # Round 0 never enters `rounds`, and it is the longest, quietest call of the run —
    # the one somebody actually goes looking for a console log about.
    council, _ = make_council(tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]})
    run(council)
    first = sorted(council.paths.calls_dir.glob("*.log"))[0]
    assert first.name.endswith("-r0.log")
    assert "# phase    : 1" in first.read_text(encoding="utf-8")


def test_capture_can_be_switched_off(tmp_path):
    council, _ = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    council.config.capture_console = False
    run(council)
    assert not council.paths.calls_dir.exists()
    state = build_state(council.paths.root)
    assert all(p["calls"] == [] for p in state["panel"])


def test_a_retry_after_a_refused_session_keeps_both_logs(tmp_path):
    from council.adapters.base import Reply

    council, adapters = make_session_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )
    a = adapters["Agent-A"]
    orig, rejected = a.ask, {"done": False}

    async def ask(prompt, cwd, timeout, session=None, on_delta=None, call_log=None):
        if session and not rejected["done"]:
            rejected["done"] = True
            if call_log is not None:
                call_log.start(["scripted"], cwd)
                call_log.finish(1, 0.0, "session not found")
            return Reply(ok=False, error="session not found")
        return await orig(prompt, cwd, timeout, session, call_log=call_log)

    a.ask = ask
    run(council)

    logs = {p["label"]: p["calls"] for p in build_state(council.paths.root)["panel"]}
    # Two calls for round 1: the refused resume and the cold retry that replaced it.
    round_one = [c for c in logs["Agent-A"] if c["round"] == 1]
    assert len(round_one) == 2
    assert [c["ok"] for c in round_one] == [False, True]
    assert len({c["file"] for c in round_one}) == 2  # neither overwrote the other
    assert "session not found" in (
        council.paths.calls_dir / round_one[0]["file"]
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["independent", "consult"])
def test_the_console_log_survives_a_panelist_that_never_answers(tmp_path, mode):
    from council.adapters.base import Reply

    council, adapters = make_council(
        tmp_path,
        {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG], "c": ["plan c", READY_MSG]},
        min_rounds=1,
        mode=mode,
    )
    failing = adapters["Agent-C"]

    async def ask(prompt, cwd, timeout, session=None, on_delta=None, call_log=None):
        if call_log is not None:
            call_log.start(["scripted", "--slow"], cwd)
            call_log.write("out", "read council/state.py")
            call_log.finish(None, 900.0, "timed out after 900s")
        return Reply(ok=False, error="timed out after 900s")

    failing.ask = ask
    run(council)  # two panelists is still a quorum, so the council finishes without it

    # The turn produced no envelope, no text and no verdict. What it was doing is here.
    logs = sorted(council.paths.calls_dir.glob("*agent-c*.log"))
    assert logs, "a panelist that only ever timed out still left its console log"
    text = logs[0].read_text(encoding="utf-8")
    assert "read council/state.py" in text and "timed out after 900s" in text
