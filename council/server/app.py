"""The HTTP surface: commands over REST, everything live over SSE.

Commands are ordinary POSTs, so they get status codes and error bodies. Streaming goes
one way — the daemon narrating — which is exactly what Server-Sent Events are for, and
in exchange the protocol hands us the two things this UI needs for free: automatic
reconnection and `Last-Event-ID` replay. The session log is already an append-only
sequence, so "resume from N" is a file read rather than a cache.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .. import PACKAGE_ROOT, __version__
from ..catalog import harness_status
from ..adapters import AdapterError
from ..config import ConfigError
from ..control import ControlError
from ..orchestrator import MODES, CouncilError
from .hub import Hub
from .idle import Idle, watch
from .registry import Registry, RegistryError, build_spec, defaults_for_form
from .security import TOKEN_COOKIE, TOKEN_QUERY, Guard

WEB_DIR = PACKAGE_ROOT / "council" / "web"

#: The only file names the console-log route will open. Must keep matching what
#: `council.calls.call_filename` produces.
CALL_FILE = re.compile(r"^\d+-[a-z0-9\-]+-r\d+\.log$")

#: Asset file names carry a hash of their own contents, so a changed file is a changed
#: URL and the old one can be kept forever without ever being wrong.
ASSET_CACHE = "public, max-age=31536000, immutable"

#: `index.html` is the one file whose name never changes, and it is what points at those
#: hashed names — so it has to be asked for every time. Without this the response carries
#: no `Cache-Control` at all, and a browser is then free to guess a freshness lifetime
#: from `Last-Modified` (usually a tenth of the file's age). Rebuild the UI and refresh,
#: and the guess is what you get: the old application, from cache, for a quarter of an
#: hour, with no way to tell that is what happened.
INDEX_CACHE = "no-cache"

#: Silence between events after which a comment frame is sent, so proxies and sleeping
#: laptops do not quietly drop an idle stream.
KEEPALIVE_SECONDS = 15.0

#: How often an idle stream wakes to notice the session has ended and close itself.
POLL_SECONDS = 0.5


def create_app(
    token: str,
    registry: Registry | None = None,
    extra_origins: tuple[str, ...] = (),
    idle: Idle | None = None,
) -> FastAPI:
    guard = Guard(token, extra_origins)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry or Registry()
        app.state.hub = Hub()
        app.state.registry.on_change = app.state.hub.publish
        app.state.idle = idle
        watchdog = None
        if idle is not None:
            # Asked live rather than tracked, so nothing can leave the daemon believing
            # a council is still going after the thing that was going has gone.
            idle.busy = lambda: any(
                runtime.state in ("starting", "running")
                for runtime in app.state.registry.sessions.values()
            )
            watchdog = asyncio.create_task(watch(idle))
        yield
        if watchdog is not None:
            watchdog.cancel()
        await app.state.registry.shutdown()

    app = FastAPI(title="Plan Council", version=__version__, lifespan=lifespan)

    def gate(request: Request) -> None:
        guard.check(request)

    protected = [Depends(gate)]

    def reg(request: Request) -> Registry:
        return request.app.state.registry

    # ---- meta ------------------------------------------------------------

    @app.get("/api/health", dependencies=protected)
    async def health(request: Request) -> dict:
        import os

        return {"app": "council", "version": __version__, "pid": os.getpid()}

    @app.get("/api/catalog", dependencies=protected)
    async def catalog() -> dict:
        # Blocking: `opencode models` shells out. Off the event loop so a slow or
        # missing opencode cannot stall every other request.
        defaults = defaults_for_form()
        binaries = {
            entry["adapter"]: entry["binary"]
            for entry in defaults["panel"]
            if entry.get("binary")
        }
        harnesses = await asyncio.to_thread(harness_status, binaries)
        # `defaults` is what a council gets when the form is left alone. Sending it
        # lets the form show what it is about to do instead of asking for trust, and
        # means council.yaml stays the single owner of those numbers.
        return {"harnesses": harnesses, "modes": list(MODES), "defaults": defaults}

    # ---- projects --------------------------------------------------------

    @app.get("/api/projects", dependencies=protected)
    async def list_projects(request: Request) -> list[dict]:
        return reg(request).list_projects()

    @app.post("/api/projects", dependencies=protected)
    async def add_project(request: Request) -> dict:
        payload = await _json_body(request)
        directory = payload.get("dir")
        if not directory:
            raise HTTPException(400, "dir is required")
        try:
            key = reg(request).register_project(directory)
        except RegistryError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.app.state.hub.publish()
        return {"dir": key}

    # ---- sessions --------------------------------------------------------

    @app.get("/api/sessions", dependencies=protected)
    async def list_sessions(request: Request, project: str | None = None) -> list[dict]:
        return reg(request).list_sessions(project)

    @app.post("/api/sessions", dependencies=protected, status_code=201)
    async def create_session(request: Request) -> dict:
        payload = await _json_body(request)
        registry = reg(request)
        try:
            # Validated *before* the project is registered: a rejected request used to
            # widen the set of directories agents may be run in anyway, and an empty
            # `project_dir` registered the daemon's own working directory.
            spec = build_spec(payload)
            if payload.get("register_project"):
                registry.register_project(spec.project_dir)
            runtime = registry.create(spec)
        except RegistryError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (AdapterError, CouncilError, ConfigError, OSError, ValueError) as exc:
            # A bad adapter name or an unreadable scenario used to escape as a 500 and
            # leave a session directory and an index entry behind — a row that listed
            # as "starting" forever and could never run.
            registry.forget(getattr(exc, "session_id", None))
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        request.app.state.hub.publish()
        return {
            "id": runtime.id,
            "dir": str(runtime.dir),
            "mode": spec.mode,
            # Labels included: every control action and every read endpoint keys on
            # `Agent-A`, and anonymisation shuffles which name that is, so a client
            # had no way to address a panelist from this response alone.
            "panel": [
                {
                    "label": p.label,
                    "name": p.name,
                    "adapter": p.adapter,
                    "model": p.model,
                }
                for p in (runtime.council.panel if runtime.council else [])
            ],
        }

    @app.get("/api/sessions/{session_id}", dependencies=protected)
    async def get_session(request: Request, session_id: str) -> dict:
        return _view(request, session_id).snapshot()

    @app.delete("/api/sessions/{session_id}", dependencies=protected)
    async def delete_session(request: Request, session_id: str) -> dict:
        try:
            reg(request).delete(session_id)
        except RegistryError as exc:
            # 409 means "running, stop it first". An id that does not exist is a 404,
            # or a client retrying on 409 retries for ever.
            status = 409 if "stop the session" in str(exc) else 404
            raise HTTPException(status, str(exc)) from exc
        request.app.state.hub.publish()
        return {"deleted": session_id}

    @app.post("/api/sessions/{session_id}/control", dependencies=protected)
    async def control(request: Request, session_id: str) -> dict:
        payload = await _json_body(request)
        action = payload.pop("action", None)
        by = payload.pop("by", "user")
        if not action:
            raise HTTPException(400, "action is required")
        view = _view(request, session_id)
        try:
            if action == "stop" and hasattr(view, "stop"):
                if view.finished:
                    # Every other action 409s on a finished session; stop returned 200
                    # and did nothing, so a UI that greys out on 409 left it enabled.
                    raise RegistryError(f"council {session_id} has already finished")
                record = await view.stop(payload.get("how", "graceful"))
            else:
                record = view.control(action, by=by, **payload)
        except ControlError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RegistryError as exc:
            raise HTTPException(409, str(exc)) from exc
        except TypeError as exc:  # wrong payload shape for this action
            raise HTTPException(400, _explain_control(action, exc)) from exc
        request.app.state.hub.publish()
        return record

    @app.get("/api/sessions/{session_id}/agents/{label}", dependencies=protected)
    async def agent_thread(request: Request, session_id: str, label: str) -> dict:
        return _view(request, session_id).agent_thread(label)

    @app.get("/api/sessions/{session_id}/digest", dependencies=protected)
    async def digest(request: Request, session_id: str) -> PlainTextResponse:
        return _artefact(_view(request, session_id).dir / "digest.md")

    @app.get("/api/sessions/{session_id}/transcript", dependencies=protected)
    async def transcript(request: Request, session_id: str) -> PlainTextResponse:
        return _artefact(_view(request, session_id).dir / "transcript.md")

    @app.get("/api/sessions/{session_id}/plans/{label}", dependencies=protected)
    async def plan(request: Request, session_id: str, label: str) -> PlainTextResponse:
        letter = label.rsplit("-", 1)[-1].lower()
        if not letter.isalpha():
            raise HTTPException(400, "not a panelist label")
        return _artefact(_view(request, session_id).dir / "plans" / f"agent-{letter}.md")

    @app.get("/api/sessions/{session_id}/calls/{name}", dependencies=protected)
    async def call_log(
        request: Request, session_id: str, name: str, offset: int = 0
    ) -> dict:
        """One console log, or the part of it the caller has not read yet.

        `offset` exists because a running call is polled: reading a 2 MiB file whole,
        every few seconds, synchronously on the event loop the harness pumps run on, is
        far more expensive than everything the capture side does per line. With it a
        poll that finds nothing new costs a seek and a stat.
        """
        # Matched against the exact shape `calls.call_filename` mints, not sanitised.
        # The set of valid names is small and known, so a pattern is both simpler and
        # stricter — the same reasoning, and the same class of hole, as `_check_session_id`.
        if not CALL_FILE.match(name):
            raise HTTPException(400, f"not a call log name: {name!r}")
        path = _view(request, session_id).dir / "calls" / name
        if not path.is_file():
            raise HTTPException(404, f"no such call log: {name}")
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, offset))
                chunk = handle.read()
                end = handle.tell()
        except OSError as exc:
            raise HTTPException(500, f"could not read {name}: {exc}") from exc
        # `errors="replace"`: a seek can land mid-character, and a mangled glyph at the
        # seam is better than refusing to show a log at all.
        return {"offset": end, "text": chunk.decode("utf-8", "replace")}

    # ---- streams ---------------------------------------------------------

    @app.get("/api/sessions/{session_id}/events", dependencies=protected)
    async def session_events(request: Request, session_id: str, from_seq: int = 0):
        view = _view(request, session_id)
        start = _resume_point(request, from_seq)
        return _sse(_session_stream(view, start, request), idle)

    @app.get("/api/events", dependencies=protected)
    async def daemon_events(request: Request):
        return _sse(_daemon_stream(request), idle)

    # ---- the app itself --------------------------------------------------

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(request: Request, path: str) -> Response:
        # The SPA catch-all must not swallow a mistyped API path and answer it with a
        # page: a client asking for JSON deserves a 404, not HTML that parses as none.
        if path.startswith("api/"):
            raise HTTPException(404, f"no such endpoint: /{path}")

        # The token arrives once, on the URL `council up` prints, and is exchanged for
        # a cookie so it never sits in the address bar while the app is in use.
        supplied = request.query_params.get(TOKEN_QUERY)
        if supplied:
            guard.check_origin(request)
            if supplied != token:
                return HTMLResponse(_message("Wrong token."), status_code=401)
            response = RedirectResponse(f"/{path}", status_code=303)
            response.set_cookie(
                TOKEN_COOKIE, token, httponly=True, samesite="strict", path="/"
            )
            return response

        asset = _asset(path)
        if asset is not None:
            return FileResponse(asset, headers={"Cache-Control": ASSET_CACHE})
        index = WEB_DIR / "index.html"
        if not index.is_file():
            return HTMLResponse(_message(_NOT_BUILT), status_code=503)
        return FileResponse(index, headers={"Cache-Control": INDEX_CACHE})

    # ---- helpers ---------------------------------------------------------

    def _view(request: Request, session_id: str):
        try:
            return reg(request).get(session_id)
        except RegistryError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app


# ---- streaming -----------------------------------------------------------


async def _counted(generator, idle: Idle):
    """The same stream, with the daemon told that somebody is on the other end of it.

    Registered before the first frame and released in `finally`, so a client that
    disappears without a word — a closed laptop, a killed tab — still decrements. Every
    open tab holds one of these, which is how the daemon knows the difference between
    quiet and abandoned.
    """
    idle.opened()
    try:
        async for chunk in generator:
            yield chunk
    finally:
        idle.closed()


def _sse(generator, idle: Idle | None = None) -> StreamingResponse:
    return StreamingResponse(
        _counted(generator, idle) if idle is not None else generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _frame(record: dict) -> str:
    return (
        f"id: {record.get('seq', 0)}\n"
        f"event: council\n"
        f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
    )


def _resume_point(request: Request, from_seq: int) -> int:
    """`Last-Event-ID` is what the browser sends on an automatic reconnect."""
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        return int(header)
    return max(from_seq, 0)


async def _session_stream(view, from_seq: int, request: Request):
    # Subscribe *before* replaying: an event emitted between the read and the
    # subscription would otherwise fall through the gap and never be delivered.
    sub = view.subscribe(from_seq) if view.live else None
    try:
        last = from_seq
        for record in view.replay(from_seq):
            last = max(last, record.get("seq", last))
            yield _frame(record)

        if sub is None or view.finished:
            # Nothing more is coming. Close rather than hold the connection open:
            # a finished session's stream that never ends is a hung client.
            yield "event: end\ndata: {}\n\n"
            return

        since_keepalive = 0.0
        while not await request.is_disconnected():
            record = await sub.get(timeout=POLL_SECONDS)
            if record is None:
                if view.finished:
                    yield "event: end\ndata: {}\n\n"
                    return
                since_keepalive += POLL_SECONDS
                if since_keepalive >= KEEPALIVE_SECONDS:
                    since_keepalive = 0.0
                    yield ": keepalive\n\n"
                continue
            since_keepalive = 0.0
            if sub.lagged:
                # Honest about it: the client refetches rather than believing a
                # snapshot it silently has holes in.
                sub.lagged = False
                yield 'event: resync\ndata: {"reason":"slow client"}\n\n'
                continue
            seq = record.get("seq", 0)
            if seq <= last:
                continue  # already delivered in the replay above
            last = seq
            yield _frame(record)
    finally:
        if sub is not None:
            sub.close()


async def _daemon_stream(request: Request):
    """Session-list changes, so a run started from the CLI appears in the browser."""
    hub: Hub = request.app.state.hub
    registry: Registry = request.app.state.registry
    with hub.subscribe() as sub:
        while not await request.is_disconnected():
            payload = {"sessions": registry.list_sessions()}
            yield f"event: sessions\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if not await sub.wait(KEEPALIVE_SECONDS):
                yield ": keepalive\n\n"


# ---- static --------------------------------------------------------------


def _asset(path: str) -> Path | None:
    """Resolve a request path inside the built UI, refusing to escape it."""
    if not path or path.endswith("/"):
        return None
    candidate = (WEB_DIR / path).resolve()
    try:
        candidate.relative_to(WEB_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _artefact(path: Path) -> PlainTextResponse:
    if not path.is_file():
        raise HTTPException(404, f"not written yet: {path.name}")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


#: What each control needs beyond its name, for when the request omits it.
_CONTROL_ARGS = {
    "skip": "agent, e.g. {\"action\": \"skip\", \"agent\": \"Agent-A\"}",
    "drop": "agent, e.g. {\"action\": \"drop\", \"agent\": \"Agent-A\"}",
    "restore": "agent, e.g. {\"action\": \"restore\", \"agent\": \"Agent-A\"}",
    "chair": "text, e.g. {\"action\": \"chair\", \"text\": \"stay off the database\"}",
    "extend": "a limit, e.g. {\"action\": \"extend\", \"max_rounds\": 8}",
    "stop": "optionally how='graceful' or how='hard'",
}


def _explain_control(action: str, exc: TypeError) -> str:
    """Say what the action wants, rather than echoing a Python signature at the user."""
    wants = _CONTROL_ARGS.get(action)
    if wants:
        return f"'{action}' needs {wants}"
    return f"'{action}' does not take those arguments ({exc})"


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(400, "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    return payload


_NOT_BUILT = (
    "The web UI has not been built.<br><br>"
    "<code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code><br><br>"
    "The API is running regardless — <code>council status</code> and the rest of the "
    "CLI work without it."
)


def _message(body: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Council</title>"
        "<style>body{background:#12100d;color:#ece4d5;font:16px/1.6 Georgia,serif;"
        "display:grid;place-items:center;height:100vh;margin:0;text-align:center}"
        "div{max-width:34rem;padding:2rem}code{color:#d9a441;font-size:.9em}</style>"
        f"<div>{body}</div>"
    )
