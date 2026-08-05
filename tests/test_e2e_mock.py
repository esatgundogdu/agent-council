"""End-to-end run of the shipped CLI against the mock panel."""

import json

from council.__main__ import main


def _task(tmp_path, text="Add a rate limiter to the API.\n\tTab and unicode ✓\n"):
    task = tmp_path / "task.md"
    task.write_text(text, encoding="utf-8")
    return task


def test_mock_run_produces_the_full_output_tree(tmp_path, capsys):
    project = tmp_path / "repo"
    project.mkdir()
    task = _task(tmp_path)

    code = main(
        ["run", "--task", str(task), "--project-dir", str(project), "--mock", "--quiet"]
    )
    assert code == 0

    sessions = list((project / ".council").iterdir())
    assert len(sessions) == 1
    session = sessions[0]

    for name in ("task.md", "transcript.md", "digest.md", "events.jsonl", "status.json"):
        assert (session / name).is_file(), f"missing {name}"

    plans = sorted((session / "plans").glob("agent-*.md"))
    assert len(plans) >= 2

    # The task must reach the panel exactly as the user wrote it.
    assert (session / "task.md").read_bytes() == task.read_bytes()

    status = json.loads((session / "status.json").read_text())
    assert status["state"] == "done"

    events = [json.loads(l) for l in (session / "events.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "session_created"
    assert kinds[-1] == "session_end"
    assert "plan_received" in kinds and "turn_end" in kinds

    digest = (session / "digest.md").read_text()
    assert "# Council digest" in digest
    assert "Final position of each panelist" in digest
    assert str(session / "digest.md") in capsys.readouterr().out


def test_task_file_inside_the_session_dir_is_not_clobbered(tmp_path):
    """The /council command writes task.md itself, then points the run at it."""
    project = tmp_path / "repo"
    session = project / ".council" / "preset"
    session.mkdir(parents=True)
    original = "Do the thing, verbatim.\n"
    (session / "task.md").write_text(original, encoding="utf-8")

    code = main(
        [
            "run",
            "--task", str(session / "task.md"),
            "--project-dir", str(project),
            "--session-dir", str(session),
            "--mock", "--quiet",
        ]
    )
    assert code == 0
    assert (session / "task.md").read_text() == original


def test_max_rounds_override_is_respected(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mock", "--quiet", "--max-rounds", "1",
        ]
    )
    assert code == 0
    session = next((project / ".council").iterdir())
    events = [json.loads(l) for l in (session / "events.jsonl").read_text().splitlines()]
    assert max(e.get("round", 0) for e in events) == 1


def test_missing_task_file_is_a_config_error(tmp_path):
    code = main(
        ["run", "--task", str(tmp_path / "nope.md"), "--project-dir", str(tmp_path), "--mock"]
    )
    assert code == 3


def test_consult_run_writes_a_digest_and_no_plans(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Already rejected: an in-process LRU.\n", encoding="utf-8")

    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mode", "consult", "--context", str(brief),
            "--mock", "--quiet",
        ]
    )
    assert code == 0

    session = next((project / ".council").iterdir())
    assert (session / "context.md").read_text(encoding="utf-8").startswith("Already rejected")
    assert not list((session / "plans").glob("*.md"))
    digest = (session / "digest.md").read_text(encoding="utf-8")
    assert "the panel started from a brief" in digest
    assert "the opening round was answered in parallel" in digest


def test_one_round_consult_is_reached_through_max_rounds(tmp_path):
    """Yesterday's whole mode is today's flag — and it must still behave the same."""
    project = tmp_path / "repo"
    project.mkdir()
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mode", "consult", "--max-rounds", "1",
            "--mock", "--quiet",
        ]
    )
    assert code == 0

    session = next((project / ".council").iterdir())
    digest = (session / "digest.md").read_text(encoding="utf-8")
    assert "no panelist saw any other's answer" in digest
    events = [json.loads(l) for l in (session / "events.jsonl").read_text().splitlines()]
    assert [e for e in events if e["event"] == "round_start"] != []
    assert max(e.get("round", 0) for e in events) == 1


def test_a_missing_brief_stops_the_run_before_it_costs_anything(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--context", str(tmp_path / "nope.md"),
            "--mock", "--quiet",
        ]
    )
    assert code == 3
    assert not (project / ".council").exists()


def test_seed_and_context_cannot_both_read_stdin(tmp_path, capsys):
    project = tmp_path / "repo"
    project.mkdir()
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mode", "review", "--seed", "-", "--context", "-",
            "--mock", "--quiet",
        ]
    )
    assert code == 3
    assert "cannot both read stdin" in capsys.readouterr().err


def test_a_proposal_is_refused_by_a_mode_that_would_never_show_it(tmp_path, capsys):
    project = tmp_path / "repo"
    project.mkdir()
    seed = tmp_path / "plan.md"
    seed.write_text("a plan nobody would read\n", encoding="utf-8")
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mode", "independent", "--seed", str(seed),
            "--mock", "--quiet",
        ]
    )
    assert code == 3
    assert "is not read by --mode independent" in capsys.readouterr().err


def test_scenario_file_drives_the_mock_panel(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "default": {
                    "plan": "SCENARIO PLAN",
                    "turns": [
                        '{"comment": "scenario comment", "verdict": "READY",'
                        ' "reason": "scenario reason"}'
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "run", "--task", str(_task(tmp_path)),
            "--project-dir", str(project),
            "--mock", "--quiet", "--scenario", str(scenario),
        ]
    )
    assert code == 0
    session = next((project / ".council").iterdir())
    assert "SCENARIO PLAN" in (session / "plans" / "agent-a.md").read_text()
    assert "scenario reason" in (session / "digest.md").read_text()
