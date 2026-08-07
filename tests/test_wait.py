"""`council wait` — the blocking primitive an agent hands its waiting to.

The point of these is the difference between a stream that *ended* and a stream that
*stopped*. Both look identical to the reader, and only one of them means the council is
over. When a person is watching the lines go by, getting that wrong costs a re-run; when
the exit code is a wake-up signal, it wakes the agent onto a digest nobody has written.
"""

import pytest

from council import __main__ as cli


class FakeClient:
    """A daemon that can be told to drop the stream partway, like a real one does."""

    def __init__(self, chunks, states, session_dir="/tmp/session"):
        self.chunks = list(chunks)  # one list of records per connection
        self.states = list(states)  # the state reported after each connection ends
        self.session_dir = session_dir
        self.connects = []  # the from_seq of every connection, in order

    def events(self, session_id, from_seq=0):
        self.connects.append(from_seq)
        for record in self.chunks.pop(0) if self.chunks else []:
            yield record

    def session(self, session_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"status": state, "session": {"dir": self.session_dir}}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "RECONNECT_SECONDS", 0)


def test_a_dropped_stream_is_not_a_finished_council(capsys):
    """The one that matters. The stream stops early with the panel still arguing; the
    session's own state is what decides, so it reconnects instead of reporting done."""
    client = FakeClient(
        chunks=[
            [{"seq": 1, "event": "phase_start", "phase": 1}, {"seq": 2, "event": "round_start"}],
            [{"seq": 3, "event": "session_end", "rounds": 2}],
        ],
        states=[{"state": "running"}, {"state": "done"}],
    )

    code = cli._await(client, "s1", describe=False, deadline=0.0)

    assert code == cli.EXIT_OK
    assert client.connects == [0, 2], "should reconnect, and resume from the last seq seen"
    assert capsys.readouterr().out.count("DIGEST:") == 1


def test_waiting_is_quiet_and_says_only_where_the_digest_is(capsys):
    client = FakeClient(
        chunks=[[{"seq": 1, "event": "phase_start", "phase": 1}]],
        states=[{"state": "done"}],
        session_dir="/repo/.council/2026-01-01_120000",
    )

    assert cli._await(client, "s1", describe=False, deadline=0.0) == cli.EXIT_OK

    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["DIGEST: " + str(cli.Path("/repo/.council/2026-01-01_120000/digest.md"))]


def test_watching_still_narrates(capsys):
    client = FakeClient(
        chunks=[[{"seq": 1, "event": "phase_start", "phase": 1}]],
        states=[{"state": "done"}],
    )

    assert cli._await(client, "s1", describe=True, deadline=0.0) == cli.EXIT_OK
    assert "Phase 1" in capsys.readouterr().out


def test_a_failed_council_is_not_a_finished_one(capsys):
    client = FakeClient(
        chunks=[[{"seq": 1, "event": "session_failed", "error": "no panelist answered"}]],
        states=[{"state": "failed", "error": "no panelist answered"}],
    )

    assert cli._await(client, "s1", describe=False, deadline=0.0) == cli.EXIT_PANEL

    captured = capsys.readouterr()
    assert "no panelist answered" in captured.err
    assert "DIGEST:" not in captured.out


def test_timing_out_is_its_own_answer(capsys, monkeypatch):
    """Not a failure and not a finish: the caller's next move is to ask again."""
    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    client = FakeClient(
        chunks=[[{"seq": 1, "event": "round_start"}], [], []],
        states=[{"state": "running"}],
    )

    assert cli._await(client, "s1", describe=False, deadline=15.0) == cli.EXIT_TIMEOUT

    captured = capsys.readouterr()
    assert "still running" in captured.err
    assert "DIGEST:" not in captured.out


def test_a_dead_daemon_is_a_config_error_not_a_finish(capsys):
    class Dead(FakeClient):
        def session(self, session_id):
            raise cli.DaemonError("connection refused")

    client = Dead(chunks=[[{"seq": 1, "event": "round_start"}]], states=[{"state": "running"}])

    assert cli._await(client, "s1", describe=False, deadline=0.0) == cli.EXIT_CONFIG
    assert "DIGEST:" not in capsys.readouterr().out


def test_wait_is_wired_into_the_cli():
    assert cli.COMMANDS["wait"] is cli.cmd_wait
    args = cli.build_parser().parse_args(["wait", "2026-01-01_120000", "--timeout", "90"])
    assert (args.session, args.timeout) == ("2026-01-01_120000", 90.0)
