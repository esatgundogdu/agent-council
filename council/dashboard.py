"""Live view of a council session.

Reads the two files the orchestrator already writes — `status.json` for progress and
`events.jsonl` for everything that was said — and serves them to a browser. Nothing
here writes to the session, so watching a run can never disturb it.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import PACKAGE_ROOT

STATIC_DIR = PACKAGE_ROOT / "council" / "static"

#: A run whose heartbeat is older than this is treated as stalled rather than live.
STALE_AFTER_SECONDS = 900


def find_sessions(project_dir: Path) -> list[Path]:
    """Session directories under `.council/`, newest first."""
    root = Path(project_dir) / ".council"
    if not root.is_dir():
        return []
    sessions = [p for p in root.iterdir() if p.is_dir() and (p / "task.md").is_file()]
    return sorted(sessions, key=lambda p: p.name, reverse=True)


def pid_alive(pid: object) -> bool:
    """Whether the process that wrote status.json is still running.

    A pid can be reused, so this is a hint rather than proof; the heartbeat age in
    `build_state` is what stops a corpse from being reported as live.
    """
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by someone else
        return True
    except OSError:
        return False
    return True


def read_events(path: Path) -> list[dict]:
    """Parse events.jsonl, tolerating a half-written final line.

    The file is appended to while we read it, so the last line may be truncated.
    That is expected, not an error: it appears complete on the next poll.
    """
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _age_seconds(timestamp: object) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def build_state(session_dir: Path) -> dict:
    """Everything the dashboard shows, assembled from the session's own files."""
    session_dir = Path(session_dir)
    status = _read_json(session_dir / "status.json")
    events = read_events(session_dir / "events.jsonl")

    identities: dict[str, str] = {}
    models: dict[str, str] = {}
    order: list[str] = []
    plans: dict[str, dict] = {}
    rounds: dict[int, list[dict]] = {}
    health: list[dict] = []
    dropped: dict[str, str] = {}
    tokens_by_agent: dict[str, int] = {}
    compactions: list[dict] = []

    for event in events:
        kind = event.get("event")
        agent = event.get("agent")
        if isinstance(agent, str) and isinstance(event.get("tokens"), int):
            tokens_by_agent[agent] = tokens_by_agent.get(agent, 0) + event["tokens"]

        if kind == "session_start":
            raw_ids = event.get("identities")
            if isinstance(raw_ids, dict):
                identities = {str(k): str(v) for k, v in raw_ids.items()}
            for entry in event.get("panel") or []:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                if isinstance(label, str):
                    order.append(label)
                    if entry.get("model"):
                        models[label] = str(entry["model"])
        elif kind == "plan_received" and isinstance(agent, str):
            plans[agent] = {
                "label": agent,
                "chars": event.get("chars"),
                "seconds": event.get("seconds"),
                "tokens": event.get("tokens"),
                "session": event.get("session"),
            }
        elif kind == "turn" and isinstance(agent, str):
            rounds.setdefault(int(event.get("round") or 0), []).append(
                {
                    "label": agent,
                    "verdict": event.get("verdict"),
                    "comment": event.get("comment") or "",
                    "reason": event.get("reason") or "",
                    "seconds": event.get("seconds"),
                    "tokens": event.get("tokens"),
                    "malformed": bool(event.get("malformed")),
                    "resumed": bool(event.get("resumed")),
                    "failed": False,
                }
            )
        elif kind == "turn_failed" and isinstance(agent, str):
            round_no = int(event.get("round") or 0)
            rounds.setdefault(round_no, []).append(
                {
                    "label": agent,
                    "verdict": None,
                    "comment": "",
                    "reason": "",
                    "failed": True,
                    "note": event.get("error") or "",
                }
            )
            health.append(
                {
                    "kind": "turn_failed",
                    "agent": agent,
                    "round": round_no,
                    "detail": event.get("error") or "",
                }
            )
        elif kind == "panelist_dropped" and isinstance(agent, str):
            reason = str(event.get("reason") or "")
            dropped[agent] = reason
            health.append({"kind": "dropped", "agent": agent, "detail": reason})
        elif kind == "session_fallback" and isinstance(agent, str):
            health.append(
                {
                    "kind": "fallback",
                    "agent": agent,
                    "round": event.get("round"),
                    "detail": event.get("error") or "",
                }
            )
        elif kind == "compacted":
            compactions.append(
                {"through_round": event.get("through_round"), "agent": agent}
            )
        elif kind == "early_ready_ignored":
            health.append(
                {
                    "kind": "early_ready",
                    "round": event.get("round"),
                    "detail": "all READY before min_rounds — discussion continued",
                }
            )
        elif kind == "session_failed":
            health.append({"kind": "session_failed", "detail": event.get("error") or ""})

    # Latest verdict per panelist, for the roster.
    latest: dict[str, dict] = {}
    for round_no in sorted(rounds):
        for turn in rounds[round_no]:
            if not turn.get("failed"):
                latest[turn["label"]] = turn

    for label in identities:
        if label not in order:
            order.append(label)
    order = sorted(set(order))

    panel = []
    for label in order:
        turn = latest.get(label)
        panel.append(
            {
                "label": label,
                "name": identities.get(label),
                "model": models.get(label),
                "dropped": label in dropped,
                "drop_reason": dropped.get(label),
                "verdict": turn.get("verdict") if turn else None,
                "reason": turn.get("reason") if turn else "",
                "tokens": tokens_by_agent.get(label, 0),
                "has_plan": label in plans,
                "plan": plans.get(label),
            }
        )

    state = str(status.get("state") or "unknown")
    heartbeat = _age_seconds(status.get("updated_at"))
    live = (
        state == "running"
        and pid_alive(status.get("pid"))
        and (heartbeat is None or heartbeat < STALE_AFTER_SECONDS)
    )
    if state == "running" and not live:
        state = "interrupted"

    task_path = session_dir / "task.md"
    digest_path = session_dir / "digest.md"

    return {
        "session": {
            "name": session_dir.name,
            "dir": str(session_dir),
            "task": task_path.read_text(encoding="utf-8") if task_path.is_file() else "",
        },
        "status": {
            "state": state,
            "raw_state": status.get("state"),
            "live": live,
            "phase": status.get("phase"),
            "round": status.get("round"),
            "elapsed": status.get("elapsed"),
            "tokens": status.get("tokens"),
            "rounds": status.get("rounds"),
            "termination": status.get("termination"),
            "error": status.get("error"),
            "heartbeat_age": heartbeat,
        },
        "panel": panel,
        "rounds": [
            {"round": n, "turns": rounds[n]} for n in sorted(rounds) if n
        ],
        "health": health,
        "compactions": compactions,
        "has_digest": digest_path.is_file(),
        "digest": digest_path.read_text(encoding="utf-8") if digest_path.is_file() else "",
    }


class _Handler(BaseHTTPRequestHandler):
    project_dir = Path(".")
    fixed_session: Path | None = None

    def _resolve_session(self) -> Path | None:
        if self.fixed_session is not None:
            return self.fixed_session
        sessions = find_sessions(self.project_dir)
        return sessions[0] if sessions else None

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - required name
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = (STATIC_DIR / "dashboard.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            session = self._resolve_session()
            if session is None:
                payload = {
                    "empty": True,
                    "message": f"No council sessions yet under {self.project_dir}/.council/",
                }
            else:
                try:
                    payload = build_state(session)
                except OSError as exc:  # session deleted mid-poll
                    payload = {"empty": True, "message": str(exc)}
            self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        if path == "/api/sessions":
            names = [p.name for p in find_sessions(self.project_dir)]
            self._send(
                200,
                json.dumps(names).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, *args) -> None:
        """Silence per-request logging; the dashboard polls once a second."""


def _free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(
    project_dir: Path,
    session: Path | None = None,
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    handler = type(
        "CouncilHandler",
        (_Handler,),
        {"project_dir": Path(project_dir), "fixed_session": session},
    )
    port = _free_port(port)
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)

    scope = session.name if session else "latest session (follows new runs)"
    print(f"Council dashboard: {url}\nWatching: {scope}\nCtrl+C to stop.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
