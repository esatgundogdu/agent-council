"""The daemon's HTTP surface, driven end to end against a mock panel."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from council.server.app import create_app
from council.server.registry import Registry, RegistryError, build_spec

TOKEN = "test-token"
LOCAL = "http://127.0.0.1:8787"

MOCK_PANEL = [
    {"name": "alpha", "adapter": "mock"},
    {"name": "beta", "adapter": "mock"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A daemon whose registry state lives under tmp_path, not the real ~/.council."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr("council.server.registry.STATE_DIR", state)
    monkeypatch.setattr("council.server.registry.REGISTRY_FILE", state / "registry.json")
    app = create_app(token=TOKEN, registry=Registry())
    # base_url matters: the guard refuses any Host that is not local, and TestClient
    # otherwise sends "testserver".
    with TestClient(app, base_url=LOCAL) as test_client:
        test_client.headers.update({"X-Council-Token": TOKEN})
        yield test_client


@pytest.fixture()
def project(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    return path


def start(client, project, **overrides):
    payload = {
        "project_dir": str(project),
        "register_project": True,
        "task": "Add a rate limiter.",
        "panel": MOCK_PANEL,
        "protocol": {"min_rounds": 1, "max_rounds": 1},
        **overrides,
    }
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def wait_for(client, session_id, states=("done", "failed"), timeout=30.0):
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = client.get(f"/api/sessions/{session_id}").json()["status"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} stuck in '{state}', wanted {states}")


# ---- the gate -----------------------------------------------------------


def test_health_needs_no_token(tmp_path, monkeypatch):
    monkeypatch.setattr("council.server.registry.STATE_DIR", tmp_path)
    monkeypatch.setattr("council.server.registry.REGISTRY_FILE", tmp_path / "r.json")
    with TestClient(create_app(token=TOKEN, registry=Registry()), base_url=LOCAL) as bare:
        assert bare.get("/api/health").json()["app"] == "council"
        assert bare.get("/api/sessions").status_code == 401


def test_a_wrong_token_is_refused(client):
    assert client.get("/api/sessions", headers={"X-Council-Token": "nope"}).status_code == 401


def test_a_foreign_origin_is_refused(client):
    response = client.get("/api/sessions", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_rebinding_host_is_refused(client):
    response = client.get("/api/health", headers={"Host": "council.evil.example"})
    assert response.status_code == 403


def test_agents_cannot_be_run_in_an_unregistered_project(client, project):
    response = client.post(
        "/api/sessions",
        json={"project_dir": str(project), "task": "hi", "panel": MOCK_PANEL},
    )
    assert response.status_code == 400
    assert "not registered" in response.text


# ---- running a council --------------------------------------------------


def test_a_session_runs_to_a_digest(client, project):
    created = start(client, project)
    assert wait_for(client, created["id"]) == "done"

    state = client.get(f"/api/sessions/{created['id']}").json()
    assert state["status"]["state"] == "done"
    assert state["has_digest"] and "Council digest" in state["digest"]
    assert len(state["panel"]) == 2
    assert all(p["has_plan"] for p in state["panel"])
    assert state["rounds"] and state["rounds"][0]["turns"]

    digest = client.get(f"/api/sessions/{created['id']}/digest")
    assert digest.status_code == 200 and "Council digest" in digest.text


def test_the_task_reaches_the_panel_verbatim(client, project):
    task = "Line one.\n\tTabbed line ✓\n\nTrailing blank line follows.\n"
    created = start(client, task=task, project=project)
    wait_for(client, created["id"])
    written = (project / ".council" / created["id"] / "task.md").read_bytes()
    assert written == task.encode("utf-8")


def test_review_mode_skips_the_planning_phase(client, project):
    created = start(
        client, project, mode="review", seed="## Proposal\n\nDo the simple thing.\n"
    )
    wait_for(client, created["id"])
    state = client.get(f"/api/sessions/{created['id']}").json()
    assert state["session"]["mode"] == "review"
    assert not any(p["has_plan"] for p in state["panel"])
    assert "Do the simple thing" in state["session"]["seed"]
    assert "review — the panel critiqued a supplied proposal" in state["digest"]


def test_review_mode_requires_a_proposal(client, project):
    response = client.post(
        "/api/sessions",
        json={
            "project_dir": str(project),
            "register_project": True,
            "task": "x",
            "mode": "review",
            "panel": MOCK_PANEL,
        },
    )
    assert response.status_code == 400 and "needs a proposal" in response.text


def test_consult_mode_holds_one_round_and_answers_in_parallel(client, project):
    created = start(
        client, project, mode="consult",
        context="We rejected an in-process LRU: it thrashed under four workers.",
    )
    wait_for(client, created["id"])
    state = client.get(f"/api/sessions/{created['id']}").json()

    assert state["session"]["mode"] == "consult"
    assert not any(p["has_plan"] for p in state["panel"])
    assert len(state["rounds"]) == 1
    assert "in-process LRU" in state["session"]["context"]
    assert "no panelist saw any other's answer" in state["digest"]


def test_a_consult_may_be_given_a_proposal_but_does_not_need_one(client, project):
    created = start(client, project, mode="consult", seed="Use a token bucket.")
    wait_for(client, created["id"])
    state = client.get(f"/api/sessions/{created['id']}").json()
    assert "token bucket" in state["session"]["seed"]


def test_a_proposal_is_refused_by_a_mode_that_would_never_show_it(client, project):
    response = client.post(
        "/api/sessions",
        json={
            "project_dir": str(project),
            "register_project": True,
            "task": "x",
            "mode": "independent",
            "seed": "a plan nobody would ever read",
            "panel": MOCK_PANEL,
        },
    )
    assert response.status_code == 400 and "does not read a proposal" in response.text


def test_the_brief_is_withheld_from_the_planning_phase(client, project):
    """The same boundary `hybrid` gives a proposal, applied to the context brief."""
    created = start(
        client, project, mode="independent",
        context="DECIDED ALREADY: the cache must survive a worker restart.",
        protocol={"min_rounds": 1, "max_rounds": 1},
    )
    wait_for(client, created["id"])

    session_dir = project / ".council" / created["id"]
    prompts = [
        json.loads(line)
        for line in (session_dir / "stream.jsonl").read_text(encoding="utf-8").splitlines()
        if '"prompt"' in line
    ]
    phase1 = [p for p in prompts if p.get("phase") == 1]
    assert phase1 and not any("DECIDED ALREADY" in p["text"] for p in phase1)
    assert any("DECIDED ALREADY" in p["text"] for p in prompts if p.get("phase") == 2)


def test_hybrid_mode_plans_first_and_then_meets_the_proposal(client, project):
    created = start(
        client, project, mode="hybrid", seed="Use a token bucket.",
        protocol={"min_rounds": 1, "max_rounds": 1},
    )
    wait_for(client, created["id"])
    state = client.get(f"/api/sessions/{created['id']}").json()
    assert all(p["has_plan"] for p in state["panel"])

    # The proposal reaches the panel only in Phase 2, never in the Phase-1 prompt.
    session_dir = project / ".council" / created["id"]
    prompts = [
        json.loads(line)
        for line in (session_dir / "stream.jsonl").read_text(encoding="utf-8").splitlines()
        if '"prompt"' in line
    ]
    phase1 = [p for p in prompts if p.get("phase") == 1]
    assert phase1 and not any("token bucket" in p["text"] for p in phase1)
    assert any("token bucket" in p["text"] for p in prompts if p.get("phase") == 2)


# ---- what the UI needs --------------------------------------------------


def test_the_stream_replays_from_a_sequence_number(client, project):
    created = start(client, project)
    wait_for(client, created["id"])

    with client.stream(
        "GET", f"/api/sessions/{created['id']}/events?from_seq=0"
    ) as stream:
        body = "".join(stream.iter_text())
    assert "event: council" in body and "id: 1" in body
    assert '"session_created"' in body and '"session_end"' in body

    seqs = [int(l.split(": ", 1)[1]) for l in body.splitlines() if l.startswith("id: ")]
    assert seqs == sorted(seqs), "events must arrive in sequence order"

    with client.stream(
        "GET", f"/api/sessions/{created['id']}/events?from_seq={seqs[-2]}"
    ) as stream:
        tail = "".join(stream.iter_text())
    assert f"id: {seqs[-1]}" in tail and f"id: {seqs[0]}" not in tail


def test_turn_deltas_carry_the_live_text(client, project):
    created = start(client, project)
    wait_for(client, created["id"])
    events = [
        json.loads(line)
        for line in (project / ".council" / created["id"] / "stream.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    deltas = [e for e in events if e["event"] == "turn_delta"]
    assert any(d["kind"] == "text" for d in deltas)
    assert any(d["kind"] == "tool" for d in deltas), "exploration should be visible"


def test_each_panelists_own_thread_is_readable(client, project):
    created = start(client, project)
    wait_for(client, created["id"])
    thread = client.get(f"/api/sessions/{created['id']}/agents/Agent-A").json()
    roles = [e["role"] for e in thread["entries"]]
    assert "sent" in roles and "reply" in roles and "tool" in roles
    sent = next(e for e in thread["entries"] if e["role"] == "sent")
    assert "senior engineer" in sent["text"], "the exact prompt we sent is recorded"


def test_the_session_list_spans_projects(client, project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    start(client, project)
    client.post("/api/projects", json={"dir": str(other)})
    rows = client.get("/api/sessions").json()
    assert len(rows) == 1 and rows[0]["project_dir"] == str(project)
    assert {p["dir"] for p in client.get("/api/projects").json()} == {
        str(project), str(other)
    }


def test_a_listed_session_says_when_it_ran(client, project):
    """Councils are cheap and their tasks rhyme; the time is what tells two apart.

    Without it the list is rows of the same sentence, and picking last night's run out
    of fourteen of them is guesswork.
    """
    created = start(client, project)
    wait_for(client, created["id"])
    row = next(r for r in client.get("/api/sessions").json() if r["id"] == created["id"])
    assert row["started_at"] and row["started_at"].startswith(created["id"][:10])
    assert row["updated_at"]  # the heartbeat, for spotting a run that has gone quiet


def test_deleting_a_session_removes_it_from_the_list_and_from_disk(client, project):
    created = start(client, project)
    wait_for(client, created["id"])
    directory = Path(client.get(f"/api/sessions/{created['id']}").json()["session"]["dir"])
    assert directory.is_dir()

    assert client.delete(f"/api/sessions/{created['id']}").status_code == 200
    assert not directory.exists()
    assert created["id"] not in {r["id"] for r in client.get("/api/sessions").json()}
    assert client.get(f"/api/sessions/{created['id']}").status_code == 404


def test_catalog_reports_the_harnesses(client):
    payload = client.get("/api/catalog").json()
    assert {h["adapter"] for h in payload["harnesses"]} == {
        "codex_cli", "claude_cli", "opencode_cli"
    }
    assert "independent" in payload["modes"]


# ---- control ------------------------------------------------------------


def test_control_of_a_finished_session_is_rejected(client, project):
    created = start(client, project)
    wait_for(client, created["id"])
    response = client.post(
        f"/api/sessions/{created['id']}/control", json={"action": "pause"}
    )
    assert response.status_code == 409


def test_an_unknown_action_is_rejected(client, project):
    created = start(client, project, protocol={"min_rounds": 1, "max_rounds": 4})
    response = client.post(
        f"/api/sessions/{created['id']}/control", json={"action": "explode"}
    )
    assert response.status_code in (400, 409)
    wait_for(client, created["id"])


def _slow_scenario(tmp_path, delay=0.4):
    """A mock panel slow enough that a control command can land mid-run."""
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"default": {"delay": delay}}), encoding="utf-8")
    return str(path)


def test_a_chair_message_reaches_the_transcript(client, project, tmp_path):
    created = start(
        client,
        project,
        protocol={"min_rounds": 3, "max_rounds": 3},
        scenario=_slow_scenario(tmp_path),
    )
    response = client.post(
        f"/api/sessions/{created['id']}/control",
        json={"action": "chair", "text": "Keep it to one file."},
    )
    assert response.status_code == 200, response.text
    wait_for(client, created["id"])

    transcript = client.get(f"/api/sessions/{created['id']}/transcript").text
    assert "Keep it to one file." in transcript
    assert "### Chair" in transcript

    # A chair message is an instruction, not a vote: it must not count towards the
    # consensus that ends the debate.
    state = client.get(f"/api/sessions/{created['id']}").json()
    assert not any(p["label"] == "Chair" for p in state["panel"])
    assert any(t.get("chair") for r in state["rounds"] for t in r["turns"])


def test_pausing_holds_the_run_until_it_is_resumed(client, project, tmp_path):
    created = start(
        client,
        project,
        protocol={"min_rounds": 4, "max_rounds": 4},
        scenario=_slow_scenario(tmp_path),
    )
    sid = created["id"]
    assert client.post(
        f"/api/sessions/{sid}/control", json={"action": "pause"}
    ).status_code == 200

    assert wait_for(client, sid, states=("paused",)) == "paused"
    assert client.get(f"/api/sessions/{sid}").json()["status"]["paused"] is True

    assert client.post(
        f"/api/sessions/{sid}/control", json={"action": "resume"}
    ).status_code == 200
    assert wait_for(client, sid) == "done"


def test_stopping_still_writes_a_digest(client, project, tmp_path):
    created = start(
        client,
        project,
        protocol={"min_rounds": 5, "max_rounds": 5},
        scenario=_slow_scenario(tmp_path),
    )
    sid = created["id"]
    assert client.post(
        f"/api/sessions/{sid}/control", json={"action": "stop", "how": "graceful"}
    ).status_code == 200
    assert wait_for(client, sid) == "done"

    digest = client.get(f"/api/sessions/{sid}/digest").text
    assert "the user stopped the session" in digest


def test_a_missing_session_is_a_404(client):
    assert client.get("/api/sessions/nope").status_code == 404


def test_a_mistyped_api_path_is_a_404_not_the_app(client):
    """The SPA catch-all must not answer a JSON client with a page."""
    response = client.get("/api/nonsense")
    assert response.status_code == 404
    assert "<!doctype html" not in response.text.lower()


# ---- spec building ------------------------------------------------------


def test_spec_rejects_an_unknown_protocol_key(tmp_path):
    with pytest.raises(RegistryError, match="unknown setting"):
        build_spec(
            {
                "project_dir": str(tmp_path),
                "task": "x",
                "panel": MOCK_PANEL,
                "protocol": {"max_rounds": 2, "nonsense": 1},
            }
        )


def test_spec_rejects_a_one_member_panel(tmp_path):
    with pytest.raises(RegistryError, match="at least 2"):
        build_spec(
            {
                "project_dir": str(tmp_path),
                "task": "x",
                "panel": [{"name": "solo", "adapter": "mock"}],
            }
        )


def test_spec_defaults_come_from_council_yaml(tmp_path):
    spec = build_spec({"project_dir": str(tmp_path), "task": "x", "panel": MOCK_PANEL})
    assert spec.protocol.min_rounds >= 1
    assert spec.on_failure in ("skip_with_note", "abort")
    # A compactor named in council.yaml is not on this ad-hoc panel, so it is dropped
    # rather than failing validation.
    assert spec.protocol.compaction_panelist is None


def test_an_edited_panel_keeps_the_harness_path_from_council_yaml(tmp_path, monkeypatch):
    """Where a harness lives belongs to the machine, not to one council.

    Editing the panel in the UI sends only name/adapter/model — it has no way to know
    where codex is installed and no business asking. Without inheritance every
    panelist on an edited panel was dropped on its first turn as "executable not
    found", while the identical council.yaml panel ran fine.
    """
    from council.config import CouncilConfig, PanelistConfig, ProtocolConfig, TimeoutConfig
    from council.server.registry import _panel

    defaults = CouncilConfig(
        panel=[
            PanelistConfig(name="gpt", adapter="codex_cli", binary="/opt/*/codex"),
            PanelistConfig(name="claude", adapter="claude_cli"),
        ],
        protocol=ProtocolConfig(),
        timeouts=TimeoutConfig(),
    )

    edited = _panel(
        {
            "panel": [
                {"name": "a", "adapter": "codex_cli", "model": "gpt-5.5"},
                {"name": "b", "adapter": "claude_cli", "model": "opus"},
            ]
        },
        defaults,
    )
    assert [p.binary for p in edited] == ["/opt/*/codex", None]

    # An explicit binary in the request still wins over the inherited one.
    override = _panel(
        {
            "panel": [
                {"name": "a", "adapter": "codex_cli", "binary": "/elsewhere/codex"},
                {"name": "b", "adapter": "claude_cli"},
            ]
        },
        defaults,
    )
    assert override[0].binary == "/elsewhere/codex"


# ---- the id is a path, so it must not be allowed to be one ----------------


def test_a_session_id_cannot_escape_the_project_directory(client, project, tmp_path):
    """`DELETE /api/sessions/..%5C..%5Cx` deleted that directory and everything in it.

    The id was pasted straight into `base / ".council" / session_id`. Routing blocks
    `/`; on Windows a backslash separates paths just as well and nothing blocked it.
    An earlier run of this removed the daemon's own state directory.
    """
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete me", encoding="utf-8")

    back = chr(92)
    # Percent-encoded, so nothing normalises them away before they reach the handler.
    # A bare `..` never gets that far: the HTTP layer resolves it to the list route.
    for attack in (
        f"..{back}..{back}elsewhere",
        f"..{back}..",
        f"nope{back}..{back}..{back}elsewhere",
        "%2E%2E%5C%2E%2E%5Celsewhere",
    ):
        encoded = attack.replace(back, "%5C")
        # 405 counts as refused too: routing rejects some forms before the handler.
        refused = (400, 404, 405)
        assert client.delete(f"/api/sessions/{encoded}").status_code in refused, attack
        assert client.get(f"/api/sessions/{encoded}").status_code in refused, attack
        assert client.get(f"/api/sessions/{encoded}/digest").status_code in refused

    assert victim.is_dir() and (victim / "precious.txt").is_file()


def test_an_empty_panel_does_not_quietly_convene_the_real_one(client, project):
    """`panel: []` fell through to council.yaml and spent the subscription."""
    for bad in ([], "alpha,beta", {}, 0):
        response = client.post(
            "/api/sessions",
            json={
                "project_dir": str(project),
                "register_project": True,
                "task": "t",
                "panel": bad,
            },
        )
        assert response.status_code == 400, f"{bad!r} was accepted: {response.text}"
        assert "non-empty list" in response.text


def test_a_bad_adapter_is_a_400_and_leaves_no_session_behind(client, project):
    """It escaped as a 500 and left a row that listed as 'starting' for ever."""
    before = {row["id"] for row in client.get("/api/sessions").json()}
    response = client.post(
        "/api/sessions",
        json={
            "project_dir": str(project),
            "register_project": True,
            "task": "t",
            "panel": [
                {"name": "a", "adapter": "wat"},
                {"name": "b", "adapter": "mock"},
            ],
        },
    )
    assert response.status_code == 400
    assert {row["id"] for row in client.get("/api/sessions").json()} == before


def test_a_rejected_request_does_not_register_the_project(client, tmp_path):
    """Registration is the boundary deciding where agents may run; a 400 widened it."""
    outsider = tmp_path / "outsider"
    outsider.mkdir()
    response = client.post(
        "/api/sessions",
        json={"project_dir": str(outsider), "register_project": True, "task": "   "},
    )
    assert response.status_code == 400
    assert str(outsider) not in {p["dir"] for p in client.get("/api/projects").json()}


def test_a_control_naming_an_unknown_panelist_is_refused(client, project):
    """`drop Agent-Z` returned 200 and did nothing — as did the user's own panel name.

    The orchestrator matches the anonymous label, and anonymisation shuffles which
    name that is, so the only spelling that worked was the least guessable one.
    """
    created = start(client, project, protocol={"min_rounds": 1, "max_rounds": 6})
    labels = {p["label"] for p in created["panel"]}
    assert labels == {"Agent-A", "Agent-B"}, "create must say how to address panelists"

    for bad in ("Agent-Z", "alpha", 7, "", None):
        response = client.post(
            f"/api/sessions/{created['id']}/control",
            json={"action": "drop", "agent": bad},
        )
        assert response.status_code in (400, 409), f"{bad!r}: {response.text}"
    wait_for(client, created["id"])


def test_stop_on_a_finished_council_is_refused_like_every_other_action(client, project):
    created = start(client, project)
    wait_for(client, created["id"])
    for action in ("pause", "stop"):
        response = client.post(
            f"/api/sessions/{created['id']}/control", json={"action": action}
        )
        assert response.status_code == 409, f"{action}: {response.status_code}"


def test_deleting_an_unknown_session_is_a_404_not_a_conflict(client):
    response = client.delete("/api/sessions/2020-01-01_000000")
    assert response.status_code == 404


def test_absurd_protocol_values_are_refused(client, project):
    for protocol in ({"max_rounds": 0}, {"min_rounds": 0}, {"token_budget": -1},
                     {"max_rounds": True}, {"wall_clock_budget": 0}):
        response = client.post(
            "/api/sessions",
            json={
                "project_dir": str(project),
                "register_project": True,
                "task": "t",
                "panel": MOCK_PANEL,
                "protocol": protocol,
            },
        )
        assert response.status_code == 400, f"{protocol}: {response.text}"


def test_lowering_the_ceiling_lowers_the_floor_with_it(client, project):
    created = start(
        client, project, protocol={"min_rounds": 5, "max_rounds": 2}
    )
    wait_for(client, created["id"])
    protocol = client.get(f"/api/sessions/{created['id']}").json()["session"]["protocol"]
    assert protocol["min_rounds"] == 2 and protocol["max_rounds"] == 2
