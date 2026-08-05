"""A thin HTTP client for the daemon.

The `council` CLI and the browser talk to exactly the same API. Keeping the CLI a
client — rather than a second implementation that also knows how to run a council —
is what lets the main agent, a terminal and the UI all drive one session without any
of them being privileged.
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

from .server.daemon import DaemonError, ensure_running, read_daemon
from .server.security import TOKEN_HEADER


class Client:
    def __init__(self, port: int, token: str, timeout: float = 30.0) -> None:
        self.port = port
        self.token = token
        self.base = f"http://127.0.0.1:{port}"
        self._http = httpx.Client(
            base_url=self.base,
            headers={TOKEN_HEADER: token},
            timeout=timeout,
        )

    @classmethod
    def connect(cls, start: bool = True, port: int | None = None) -> "Client":
        record = ensure_running(port or 8787) if start else read_daemon()
        if not record.get("port") or not record.get("token"):
            raise DaemonError("no daemon is running. Start one with `council up`.")
        return cls(record["port"], record["token"])

    @property
    def url(self) -> str:
        return f"{self.base}/"

    # ---- requests --------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs):
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DaemonError(f"cannot reach the daemon at {self.base}: {exc}") from exc
        if response.status_code >= 400:
            raise DaemonError(f"{method} {path} -> {response.status_code}: {_detail(response)}")
        return response

    def get(self, path: str, **params):
        return self._call("GET", path, params={k: v for k, v in params.items() if v}).json()

    def post(self, path: str, payload: dict):
        return self._call("POST", path, json=payload).json()

    def text(self, path: str) -> str:
        return self._call("GET", path).text

    # ---- the API ---------------------------------------------------------

    def sessions(self, project: str | None = None) -> list[dict]:
        return self.get("/api/sessions", project=project)

    def create(self, payload: dict) -> dict:
        return self.post("/api/sessions", payload)

    def session(self, session_id: str) -> dict:
        return self.get(f"/api/sessions/{session_id}")

    def control(self, session_id: str, action: str, **payload) -> dict:
        return self.post(f"/api/sessions/{session_id}/control", {"action": action, **payload})

    def digest(self, session_id: str) -> str:
        return self.text(f"/api/sessions/{session_id}/digest")

    def events(self, session_id: str, from_seq: int = 0) -> Iterator[dict]:
        """Follow a session's event stream, yielding one record per event."""
        url = f"/api/sessions/{session_id}/events"
        with self._http.stream(
            "GET", url, params={"from_seq": from_seq}, timeout=None
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise DaemonError(f"GET {url} -> {response.status_code}")
            for event, data in _parse_sse(response.iter_lines()):
                if event == "end":
                    return
                if event == "council":
                    try:
                        yield json.loads(data)
                    except (json.JSONDecodeError, ValueError):
                        continue


def _parse_sse(lines) -> Iterator[tuple[str, str]]:
    """Reassemble SSE frames. `data:` may repeat within one frame; a blank line ends it."""
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue  # keepalive comment
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:400]
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return response.text[:400]
