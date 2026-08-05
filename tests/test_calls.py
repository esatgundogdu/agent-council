"""Console capture: the raw output of every harness process.

Everything else about a turn has been through a parser by the time it is stored, so
these tests care about one property above all — that what a harness actually printed
survives, including the cases where nothing else does (a timeout, a non-zero exit).
"""

import asyncio
import re
import sys
import time

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
    assert log.truncated and log.skipped > 0
    assert "omitted  :" in text  # and the footer admits it


def test_nothing_is_written_to_the_head_after_the_cap(tmp_path):
    """A short line arriving after a long one overflowed still fits under the cap.

    Written straight to the head it lands in the file *before* content that went to the
    tail moments earlier — so the log reads in an order the process never printed in.
    Found by a real panel reviewing this feature.

    The construction matters twice over. The cap has to be reached by a line too big
    for the remaining room rather than by the running total, or no later line can fit
    either. And the room left has to exceed the truncation marker plus the short line,
    or the marker itself soaks up the space and hides the bug — which is exactly what
    the first version of this test did, and it passed against the broken code.
    """
    log = CallLog(tmp_path / "c.log", tail_bytes=4_000)
    log.start(["x"], cwd=".")
    log.limit = log.written + 900  # room for the marker and then some
    log.write("out", "BIG " + "z" * 1000)  # too big for the room: overflows to the tail
    log.write("out", "SMALL")  # still fits under the cap — must not go to the head
    log.finish(0, 1.0)

    text = read(log)
    assert "BIG" in text and "SMALL" in text
    assert text.index("BIG") < text.index("SMALL"), "the file is in the printed order"


def test_the_terminal_line_survives_a_straggler_arriving_after_it(tmp_path):
    """stdout and stderr are teed from two independently scheduled readers.

    So "oldest" is not "least valuable": every harness puts the event carrying the
    answer, the token count and the session id last, and a stray stderr line arriving
    after it evicted that event outright.
    """
    log = CallLog(tmp_path / "c.log", tail_bytes=90)
    log.start(["x"], cwd=".")
    log.limit = log.written  # everything from here on goes to the tail
    log.write("out", '{"type":"result","cost":0.41,"session_id":"KEEP-ME"}')  # 52 + nl
    log.write("err", "a straggler that arrived last")  # + prefix, forces an eviction
    log.finish(0, 1.0)

    text = read(log)
    assert log.skipped > 0, "the tail really did have to give something up"
    # The front of the oldest entry is trimmed, not the whole entry dropped — so the
    # part of the terminal event that names the outcome is still there.
    assert "KEEP-ME" in text, "the terminal event was not evicted by a later stderr line"
    assert "a straggler that arrived last" in text


def test_a_live_reader_is_told_why_the_file_stopped_growing(tmp_path):
    """Past the cap the file stops growing until the call ends.

    Without a marker, a reader watching a flooding harness sees exactly what they see
    watching a hung one — which is the distinction they opened the log to make.
    """
    log = CallLog(tmp_path / "c.log", limit=900, tail_bytes=300)
    log.start(["x"], cwd=".")
    for i in range(40):
        log.write("out", "flood " + "f" * 60)
    # Mid-call: nothing has been finished, and the file already explains itself.
    text = read(log)
    assert "cap was reached here" in text
    assert "still running and still printing" in text


def test_note_appends_after_a_log_has_already_been_closed(tmp_path):
    log = CallLog(tmp_path / "c.log")
    log.start(["claude", "-p"], cwd=".")
    log.finish(0, 12.0)
    log.note("rejected", "claude returned an empty response")

    text = read(log)
    assert "# exit code: 0" in text, "the process's own verdict is left alone"
    assert text.index("exit code") < text.index("rejected"), "appended, not interleaved"
    assert "# rejected : claude returned an empty response" in text


def test_a_log_whose_header_could_not_be_written_is_never_announced(tmp_path):
    """`open("w")` succeeding leaves a zero-byte file behind.

    So announcing on open alone produces a readable-but-empty log that `finish` then
    refuses to complete — an entry the UI shows as running for ever, on a screen whose
    job is saying which calls are.
    """
    announced: list[str] = []

    good = CallLog(tmp_path / "c.log", on_open=lambda: announced.append("good"))
    good.start(["x"], cwd=".")
    assert announced == ["good"] and good.written > 0

    bad = CallLog(tmp_path / "d.log", on_open=lambda: announced.append("bad"))
    # Exactly what a disk error during the header does: the file exists, nothing is in it.
    bad._raw = lambda text: None  # type: ignore[method-assign]
    bad.start(["x"], cwd=".")
    assert announced == ["good"], "an empty log is not announced"
    assert bad.path.is_file() and bad.written == 0


def test_output_left_in_the_pipe_at_the_deadline_is_recovered(tmp_path):
    """`wait_for` cancels the pumps *before* `_kill` runs.

    So everything sitting in the OS pipe when the deadline passed was being discarded —
    up to a pipe buffer's worth, which is larger than the whole tail budget. The
    timed-out call is the one whose log is the only record there is.

    The construction: the child prints everything it has to print — comfortably inside
    one pipe buffer, so none of it is lost to the kill — and then blocks. A deliberately
    slow `on_line` means the reader is only part-way through that backlog when the
    deadline passes. Whether the last line reaches the log is then exactly the question:
    a pump cancelled at the deadline never sees it; one that is allowed to finish does.

    `on_line` is synchronous and runs on the pump's own task, which is what makes it
    usable as a brake at all.
    """
    log = CallLog(tmp_path / "c.log")
    seen: list[str] = []

    def slow(line: str) -> None:
        seen.append(line)
        time.sleep(0.003)

    script = (
        "import sys\n"
        "for i in range(600): print('line %04d ' % i + 'x' * 60)\n"  # ~42 kB, one buffer
        "sys.stdout.flush()\n"
        "import time; time.sleep(30)\n"
    )
    reply = asyncio.run(
        run_process([sys.executable, "-c", script], cwd=str(tmp_path), timeout=0.5,
                    on_line=slow, call_log=log)
    )
    assert not reply.ok and "timed out" in reply.error

    text = read(log)
    logged = [int(m) for m in re.findall(r"^line (\d{4})", text, re.M)]
    assert logged, "something was captured"
    assert logged[0] == 0, "and it starts where the process did"
    assert 599 in logged, (
        f"the log stops at line {logged[-1]}: the backlog still in the pipe when the "
        "deadline passed was thrown away with the pump that was reading it"
    )
    assert "exit code: killed" in text


def test_the_cap_counts_bytes_and_not_characters(tmp_path):
    """Every budget in this file is named and documented in bytes.

    Counting `len(str)` instead let the file on disk run past the nominal cap by up to
    4x, and made the footer and the size shown in the UI wrong by the same factor, for
    any panelist that printed non-ASCII — which is any panelist writing prose in a
    language other than English, box-drawing, or an emoji. Every earlier test wrote
    pure ASCII, where the two counts coincide, so none of them could catch it.
    """
    log = CallLog(tmp_path / "c.log", limit=4_000, tail_bytes=500)
    log.start(["x"], cwd=".")
    line = "yürüttüğü işlem — köşe: ✓ 日本語"  # 30 characters, 47 bytes
    assert len(line) < len(line.encode("utf-8"))
    for _ in range(200):
        log.write("out", line)
    log.finish(0, 1.0)

    on_disk = log.path.stat().st_size
    assert log.out_bytes == 200 * len(line.encode("utf-8"))
    # The head is capped in the same units the cap is quoted in, so the file cannot
    # overrun it by a factor of however multi-byte the output happened to be.
    assert on_disk <= 4_000 + 500 + 2_000, f"{on_disk} bytes on disk against a 4 kB cap"
    footer = log.path.read_text(encoding="utf-8")
    assert f"stdout   : {log.out_bytes:,} bytes" in footer


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


def test_a_session_recorded_before_the_open_event_still_reads_as_finished():
    """One `call_logged`, at the end, with no `done` — what the first version emitted.

    Reading the flag alone would leave every call in such a session showing as still
    running, for ever, on a screen whose whole job is saying which ones are.
    """
    from council.state import Reducer

    reducer = Reducer()
    reducer.feed(
        {
            "seq": 1,
            "event": "call_logged",
            "agent": "Agent-A",
            "round": 1,
            "phase": 2,
            "file": "0001-agent-a-r1.log",
            "seconds": 12.0,
            "exit_code": 0,
            "bytes": 4096,
            "ok": True,
        }
    )
    assert reducer.calls["Agent-A"][0]["done"] is True


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


def test_a_log_is_announced_when_it_opens_not_when_it_closes(tmp_path):
    """Otherwise it is missing for exactly the stretch anybody would open it.

    A Phase 1 call reading a large repository runs for minutes; the UI lists calls
    from `panel[].calls`, and an entry that only appears on completion is absent for
    the whole time the panelist is quiet and the user is wondering why.
    """
    seen: list[dict] = []
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}, min_rounds=1
    )

    async def ask(prompt, cwd, timeout, session=None, on_delta=None, call_log=None):
        call_log.start(["scripted"], cwd)
        # Mid-call: the file exists, the event has gone out, and nothing has finished.
        seen.append(
            {
                "calls": [
                    dict(call)
                    for member in build_state(council.paths.root)["panel"]
                    for call in member["calls"]
                ]
            }
        )
        call_log.write("out", "still working")
        call_log.finish(0, 1.0)
        return Reply(ok=True, text=READY_MSG)

    from council.adapters.base import Reply

    adapters["Agent-A"].ask = ask
    run(council)

    first = seen[0]["calls"]
    assert first, "the log was announced while its call was still running"
    assert first[0]["done"] is False
    assert first[0]["seconds"] is None

    # And the same file, folded once, with the numbers filled in.
    final = [c for p in build_state(council.paths.root)["panel"] for c in p["calls"]]
    same = [c for c in final if c["file"] == first[0]["file"]]
    assert len(same) == 1, "open and close fold onto one entry, not two"
    assert same[0]["done"] and same[0]["ok"] and same[0]["seconds"] is not None


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


def test_a_clean_exit_that_produced_nothing_usable_says_so_in_the_log(tmp_path):
    """Every adapter turns a clean exit into a failed turn when the harness returns
    nothing parseable — `claude_cli.py:91-99` and the equivalents in codex and
    opencode. By then `run_process` has closed the file at `exit code: 0` with no
    error: true of the process, false about the call, and nothing said so.
    """
    from council.adapters.base import Reply

    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )

    async def ask(prompt, cwd, timeout, session=None, on_delta=None, call_log=None):
        # Exactly the adapter shape: the process was fine, the reply was not.
        call_log.start(["claude", "-p"], cwd)
        call_log.write("out", '{"type":"result","result":""}')
        call_log.finish(0, 3.0)
        return Reply(ok=False, error="claude returned an empty response", exit_code=0)

    adapters["Agent-A"].ask = ask
    with pytest.raises(Exception):  # one panelist left is not a quorum
        run(council)

    log = sorted(council.paths.calls_dir.glob("*agent-a*.log"))[0]
    text = log.read_text(encoding="utf-8")
    assert "# exit code: 0" in text, "the process's own verdict is left alone"
    assert "claude returned an empty response" in text


def test_a_hard_stop_still_closes_out_the_call_it_interrupted(tmp_path):
    """Otherwise the only event about that log is the one saying it opened.

    `_ask` re-raises `CancelledError` straight past `_note_call`, so the interrupted
    call — the one worth reading — stayed marked `done=False` for the life of the
    session, and the UI polled a file that would never change again.
    """
    council, adapters = make_council(
        tmp_path, {"a": ["plan a", READY_MSG], "b": ["plan b", READY_MSG]}
    )

    async def ask(prompt, cwd, timeout, session=None, on_delta=None, call_log=None):
        call_log.start(["scripted"], cwd)
        call_log.write("out", "reading the repository")
        raise asyncio.CancelledError()

    adapters["Agent-A"].ask = ask
    with pytest.raises(asyncio.CancelledError):
        run(council)

    calls = [
        call
        for member in build_state(council.paths.root)["panel"]
        for call in member["calls"]
    ]
    assert calls, "the interrupted call was recorded"
    assert all(call["done"] for call in calls), "and is not left looking live for ever"
    assert not calls[0]["ok"]


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
