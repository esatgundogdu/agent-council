"""Sessions the daemon knows about — the ones it is running, and the ones on disk.

A session's artefacts stay where they always were, under `<project>/.council/<id>/`:
next to the code they are about, git-ignorable, readable without the daemon. What the
daemon adds is a user-level index (`~/.council/registry.json`) mapping session id to
project, so one window can show every council across every project.

The orchestrator runs here, as an asyncio task in the daemon's own loop. It does no
work of its own — it awaits subprocesses — so keeping it in-process makes pause, stop
and inject a method call rather than an IPC protocol. The cost is honest: restarting
the daemon ends running sessions.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .. import DEFAULT_CONFIG_PATH, OPENCODE_CONFIG_PATH, PROMPTS_DIR
from ..adapters import AdapterError
from ..catalog import CatalogError, parse_agents_argument, resolve_agents
from ..config import (
    ConfigError,
    CouncilConfig,
    PanelistConfig,
    ProtocolConfig,
    TimeoutConfig,
    load_config,
)
from ..control import Controller
from ..events import EventLog, read_events
from ..orchestrator import (
    ACCEPTS_SEED,
    MODES,
    SEEDED_MODES,
    Council,
    CouncilError,
    SessionPaths,
)
from ..panel import build_panel
from ..state import (
    Reducer,
    build_agent_thread,
    build_state,
    compose_state,
    find_sessions,
    pid_alive,
    read_json,
)

STATE_DIR = Path.home() / ".council"
REGISTRY_FILE = STATE_DIR / "registry.json"

#: Concurrent harness processes across every session this daemon runs.
DEFAULT_MAX_CONCURRENT_CALLS = 6


class RegistryError(Exception):
    """A request the registry cannot satisfy (unknown session, bad project, ...)."""


#: What `new_id` mints, and the only shape a session id may have: a timestamp, with
#: an optional `-2` when two councils start in the same second.
SESSION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?$")


def _check_session_id(session_id: str) -> None:
    """Refuse anything that is not an id this registry could have minted.

    The id was pasted straight into `base / ".council" / session_id`, so a path was
    a path: `DELETE /api/sessions/..%5C..%5Celsewhere` reached `shutil.rmtree` and
    removed that directory and everything under it, and the matching GET read
    `task.md` and `digest.md` from anywhere on disk. Routing blocks `/`; on Windows a
    backslash is just as good a separator and nothing blocked that.

    A pattern, not a sanitiser: the set of valid ids is tiny and exactly known, so
    matching it is both simpler and stricter than trying to strip what is dangerous.
    """
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        raise RegistryError(
            f"not a session id: {session_id!r}. They look like 2026-08-04_143000 — "
            "run `council sessions` for the ones that exist."
        )


@dataclass
class SessionSpec:
    """A validated request to convene a council."""

    project_dir: Path
    mode: str
    task: str
    seed: str = ""
    #: What the agent that convened this council already knows. Optional in every mode,
    #: and never shown to a panelist writing its independent plan — see
    #: `Council._context_block`.
    context: str = ""
    panel: list[PanelistConfig] = field(default_factory=list)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    on_failure: str = "skip_with_note"
    #: JSON script for the `mock` adapter — the whole protocol, no model calls. How
    #: the UI is developed without spending a subscription on every reload.
    scenario: str | None = None

    def config(self) -> CouncilConfig:
        return CouncilConfig(
            panel=self.panel,
            protocol=self.protocol,
            timeouts=self.timeouts,
            on_failure=self.on_failure,
        )


class DiskSession:
    """A session this daemon is not running: read from its files, no control."""

    live = False
    finished = True

    def __init__(self, session_id: str, session_dir: Path) -> None:
        self.id = session_id
        self.dir = Path(session_dir)

    def snapshot(self) -> dict:
        return build_state(self.dir)

    def replay(self, from_seq: int = 0) -> list[dict]:
        return read_events(self.dir, from_seq=from_seq)

    def subscribe(self):
        return None

    def agent_thread(self, label: str) -> dict:
        return build_agent_thread(self.dir, label)

    def control(self, action: str, by: str = "user", **payload) -> dict:
        raise RegistryError(
            f"session {self.id} is not running in this daemon, so it cannot be controlled"
        )


class SessionRuntime:
    """One council the daemon owns, from creation to digest."""

    live = True

    def __init__(
        self,
        session_id: str,
        spec: SessionSpec,
        paths: SessionPaths,
        call_gate: asyncio.Semaphore,
        on_change=None,
    ) -> None:
        self.id = session_id
        self.spec = spec
        self.paths = paths
        self.dir = paths.root
        self.log = EventLog(paths.root)
        self.controller = Controller()
        self.reducer = Reducer()
        self.log.add_listener(self.reducer.feed)
        self.task: asyncio.Task | None = None
        self.council: Council | None = None
        self.error: str | None = None
        self.state = "starting"
        self._call_gate = call_gate
        self._on_change = on_change or (lambda: None)

    @property
    def finished(self) -> bool:
        """Nothing more will be emitted, so a stream over this session can close."""
        return self.task is None or self.task.done()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        config = self.spec.config()
        panel = build_panel(config)
        self.council = Council(
            config=config,
            panel=panel,
            paths=self.paths,
            project_dir=self.spec.project_dir,
            prompts_dir=PROMPTS_DIR,
            opencode_config=(
                OPENCODE_CONFIG_PATH if OPENCODE_CONFIG_PATH.is_file() else None
            ),
            scenario_path=self.spec.scenario,
            mode=self.spec.mode,
            log=self.log,
            controller=self.controller,
            session_id=self.id,
            call_gate=self._call_gate,
        )
        self.state = "running"
        self.task = asyncio.create_task(self._run(), name=f"council-{self.id}")

    async def _run(self) -> None:
        try:
            await self.council.run()
            self.state = "done"
        except asyncio.CancelledError:
            self.state = "cancelled"
            self._fail_status("cancelled by the user")
            raise
        except (CouncilError, AdapterError) as exc:
            self.state = "failed"
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a daemon must survive a bad session
            self.state = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            self.log.emit("session_failed", error=self.error)
            self._fail_status(self.error)
        finally:
            self.log.close()
            self._on_change()

    def _fail_status(self, error: str) -> None:
        """Record the failure without discarding what the session already knew.

        This used to write three keys over the top of the real status file, losing
        `mode`, `pid`, `updated_at`, `tokens` and `elapsed` — so a cancelled session
        came back after a daemon restart as `failed`, with no heartbeat and its mode
        re-guessed from whether seed.md happened to exist.
        """
        try:
            record = read_json(self.paths.status)
            record.update(state=self.state, id=self.id, error=error)
            record.setdefault("mode", self.spec.mode)
            self.paths.status.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - nothing useful to do about it
            pass

    async def stop(self, how: str = "graceful") -> dict:
        """Graceful stops let the current turn finish and still write a digest."""
        record = self.controller.command("stop", how=how)
        if how == "hard" and self.task is not None and not self.task.done():
            self.task.cancel()
        return record

    # ---- reading ---------------------------------------------------------

    def snapshot(self) -> dict:
        state = compose_state(self.dir, self.reducer, read_json(self.paths.status))
        # status.json is only rewritten at phase and round boundaries, so for a session
        # this daemon owns, the runtime is the authority on whether it is alive.
        status = state["status"]
        status["paused"] = self.controller.paused
        if self.state in ("starting", "running"):
            status["state"] = "paused" if self.controller.paused else "running"
            status["live"] = True
        elif self.state in ("failed", "cancelled"):
            status["state"] = self.state
            status["live"] = False
            status["error"] = self.error or status.get("error")
        return state

    def replay(self, from_seq: int = 0) -> list[dict]:
        return self.log.replay(from_seq=from_seq)

    def subscribe(self, from_seq: int = 0):
        return self.log.subscribe(from_seq)

    def agent_thread(self, label: str) -> dict:
        return build_agent_thread(self.dir, label)

    def control(self, action: str, by: str = "user", **payload) -> dict:
        if self.task is None or self.task.done():
            raise RegistryError(f"session {self.id} is no longer running")
        return self.controller.command(action, by=by, **payload)


class Registry:
    """Every project and session the daemon can see."""

    def __init__(self, max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, SessionRuntime] = {}
        self.index: dict[str, str] = {}  # session id -> project dir
        self.projects: list[str] = []
        self.call_gate = asyncio.Semaphore(max_concurrent_calls)
        #: Replaced by the app with the hub's publish, so a session finishing wakes
        #: every open browser rather than waiting for someone to reload.
        self.on_change = lambda: None
        self._load()

    # ---- persistence -----------------------------------------------------

    def _load(self) -> None:
        data = read_json(REGISTRY_FILE)
        projects = data.get("projects")
        if isinstance(projects, list):
            self.projects = [str(p) for p in projects if isinstance(p, str)]
        index = data.get("sessions")
        if isinstance(index, dict):
            self.index = {str(k): str(v) for k, v in index.items()}

    def _save(self) -> None:
        REGISTRY_FILE.write_text(
            json.dumps({"projects": self.projects, "sessions": self.index}, indent=2),
            encoding="utf-8",
        )

    # ---- projects --------------------------------------------------------

    def register_project(self, project_dir: str | Path) -> str:
        path = Path(project_dir).expanduser().resolve()
        if not path.is_dir():
            raise RegistryError(f"not a directory: {path}")
        key = str(path)
        if key not in self.projects:
            self.projects.append(key)
            self._save()
        return key

    def require_project(self, project_dir: str | Path) -> Path:
        """A project must be registered before agents will be run inside it.

        The daemon spawns CLI agents that read whatever is in this directory. Letting
        any caller name an arbitrary path would make a stray localhost POST enough to
        point them somewhere private, so registration is a deliberate, separate act.
        """
        path = Path(project_dir).expanduser().resolve()
        if str(path) not in self.projects:
            raise RegistryError(
                f"project not registered: {path}. Register it first "
                "(POST /api/projects, or `council start` does it for you)."
            )
        if not path.is_dir():
            raise RegistryError(f"registered project has gone: {path}")
        return path

    def list_projects(self) -> list[dict]:
        out = []
        for key in self.projects:
            path = Path(key)
            out.append(
                {
                    "dir": key,
                    "name": path.name,
                    "exists": path.is_dir(),
                    "sessions": len(find_sessions(path)) if path.is_dir() else 0,
                }
            )
        return out

    # ---- sessions --------------------------------------------------------

    def new_id(self, project_dir: Path) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        candidate, n = stamp, 1
        while candidate in self.index or (project_dir / ".council" / candidate).exists():
            n += 1
            candidate = f"{stamp}-{n}"
        return candidate

    def create(self, spec: SessionSpec) -> SessionRuntime:
        project = self.require_project(spec.project_dir)
        spec.project_dir = project
        session_id = self.new_id(project)
        paths = SessionPaths(root=project / ".council" / session_id)
        paths.prepare()
        # The task reaches the panel byte-for-byte: no summary, no commentary, no framing.
        paths.task.write_text(spec.task, encoding="utf-8", newline="")
        if spec.seed.strip():
            paths.seed.write_text(spec.seed, encoding="utf-8", newline="")
        if spec.context.strip():
            paths.context.write_text(spec.context, encoding="utf-8", newline="")

        runtime = SessionRuntime(
            session_id, spec, paths, self.call_gate, on_change=lambda: self.on_change()
        )
        try:
            runtime.start()
        except Exception as exc:  # noqa: BLE001 - clean up, then let the caller answer
            # `start()` builds the panel and the adapters, so an unknown adapter or an
            # unreadable scenario raises here. Recording the session first left a row
            # that listed as "starting" forever and could never be run or explained.
            shutil.rmtree(paths.root, ignore_errors=True)
            exc.session_id = session_id  # type: ignore[attr-defined]
            raise
        self.sessions[session_id] = runtime
        self.index[session_id] = str(project)
        self._save()
        return runtime

    def forget(self, session_id: str | None) -> None:
        """Drop every trace of a session that never started."""
        if not session_id:
            return
        self.sessions.pop(session_id, None)
        if self.index.pop(session_id, None) is not None:
            self._save()

    def get(self, session_id: str):
        _check_session_id(session_id)
        runtime = self.sessions.get(session_id)
        if runtime is not None:
            return runtime
        project = self.index.get(session_id)
        candidates = [Path(project)] if project else [Path(p) for p in self.projects]
        for base in candidates:
            path = base / ".council" / session_id
            if path.is_dir():
                return DiskSession(session_id, path)
        raise RegistryError(f"no such session: {session_id}")

    def list_sessions(self, project_dir: str | Path | None = None) -> list[dict]:
        """Sessions across every registered project, newest first."""
        bases = (
            [Path(project_dir).expanduser().resolve()]
            if project_dir
            else [Path(p) for p in self.projects]
        )
        rows: list[dict] = []
        for base in bases:
            if not base.is_dir():
                continue
            for path in find_sessions(base):
                runtime = self.sessions.get(path.name)
                status = read_json(path / "status.json")
                rows.append(
                    {
                        "id": path.name,
                        "dir": str(path),
                        "project_dir": str(base),
                        "project": base.name,
                        # One vocabulary with the detail view: `SessionRuntime.snapshot`
                        # maps the same way, and a row that disagreed with the page it
                        # opens is a row nobody can trust.
                        "state": _live_state(runtime) or _resting_state(status),
                        "live": bool(runtime and not runtime.finished),
                        "paused": bool(runtime and runtime.controller.paused),
                        "mode": status.get("mode") or _mode_of(path),
                        "phase": status.get("phase"),
                        "round": status.get("round"),
                        "tokens": status.get("tokens"),
                        "elapsed": status.get("elapsed"),
                        "has_digest": (path / "digest.md").is_file(),
                        "task": _first_line(path / "task.md"),
                        "started_at": _started_at(path.name),
                        "updated_at": status.get("updated_at"),
                    }
                )
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows

    def delete(self, session_id: str) -> None:
        runtime = self.sessions.get(session_id)
        if runtime is not None and runtime.state == "running":
            raise RegistryError("stop the session before deleting it")
        view = self.get(session_id)
        shutil.rmtree(view.dir, ignore_errors=True)
        self.sessions.pop(session_id, None)
        self.index.pop(session_id, None)
        self._save()

    async def shutdown(self) -> None:
        for runtime in list(self.sessions.values()):
            if runtime.task is not None and not runtime.task.done():
                runtime.task.cancel()
        for runtime in list(self.sessions.values()):
            if runtime.task is not None:
                try:
                    await runtime.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


# ---- spec building -------------------------------------------------------


def build_spec(
    payload: dict, config_path: str | Path | None = None
) -> SessionSpec:
    """Turn an API request into a validated spec, defaulting from council.yaml.

    Everything the form does not say is inherited from the file, so the UI and the
    committed defaults never disagree about what a "normal" council looks like.
    """
    defaults = _defaults(config_path)

    mode = str(payload.get("mode") or "independent")
    if mode not in MODES:
        raise RegistryError(f"unknown mode '{mode}'. Known: {', '.join(MODES)}")

    task = payload.get("task")
    if not isinstance(task, str) or not task.strip():
        raise RegistryError("task is required and cannot be empty")

    seed = payload.get("seed") or ""
    if not isinstance(seed, str):
        raise RegistryError("seed must be text")
    if mode in SEEDED_MODES and not seed.strip():
        raise RegistryError(f"mode '{mode}' needs a proposal: pass `seed`")
    if seed.strip() and mode not in ACCEPTS_SEED:
        # Accepting it wrote a seed.md no prompt would ever contain — an artefact
        # claiming a proposal was reviewed when the panel never saw one.
        raise RegistryError(
            f"mode '{mode}' does not read a proposal. Modes that do: "
            f"{', '.join(ACCEPTS_SEED)}."
        )

    context = payload.get("context") or ""
    if not isinstance(context, str):
        raise RegistryError("context must be text")

    panel = _panel(payload, defaults)
    protocol = _section(ProtocolConfig, payload.get("protocol"), defaults.protocol)
    timeouts = _section(TimeoutConfig, payload.get("timeouts"), defaults.timeouts)
    _check_limits(protocol, timeouts)

    names = {p.name for p in panel}
    if protocol.compaction_panelist not in names:
        # A compactor named in council.yaml need not be on an ad-hoc panel.
        protocol.compaction_panelist = None

    on_failure = payload.get("on_failure") or defaults.on_failure
    if on_failure not in ("skip_with_note", "abort"):
        raise RegistryError("on_failure must be 'skip_with_note' or 'abort'")

    project_dir = payload.get("project_dir")
    if not project_dir:
        raise RegistryError("project_dir is required")

    return SessionSpec(
        project_dir=Path(str(project_dir)),
        mode=mode,
        task=task,
        seed=seed,
        context=context,
        panel=panel,
        protocol=protocol,
        timeouts=timeouts,
        on_failure=on_failure,
        scenario=payload.get("scenario") or None,
    )


def default_panel(config_path: str | Path | None = None) -> list[dict]:
    """The panel from council.yaml, for a form that offers to use it.

    A checkbox saying "use council.yaml" tells the user nothing about who will read
    their repository. This is what it means, in the same shape the form edits.
    """
    return [
        {"name": p.name, "adapter": p.adapter, "model": p.model, "binary": p.binary}
        for p in _defaults(config_path).panel
    ]


def defaults_for_form(config_path: str | Path | None = None) -> dict:
    """Everything a council gets when the form is left alone.

    The form used to carry its own copy of these numbers and send them on every
    request, which silently overrode council.yaml: editing the file changed what the
    CLI did and nothing about what the UI did. Now one side owns them, this sends
    them for display, and the form transmits only what the user actually changed.
    """
    config = _defaults(config_path)
    return {
        "panel": default_panel(config_path),
        "protocol": asdict(config.protocol),
        "timeouts": asdict(config.timeouts),
        "on_failure": config.on_failure,
    }


def _defaults(config_path: str | Path | None) -> CouncilConfig:
    try:
        return load_config(config_path or DEFAULT_CONFIG_PATH)
    except ConfigError:
        # A missing or broken council.yaml must not stop the UI from starting a
        # council whose panel the request names in full.
        return CouncilConfig(panel=[], protocol=ProtocolConfig(), timeouts=TimeoutConfig())


def _panel(payload: dict, defaults: CouncilConfig) -> list[PanelistConfig]:
    agents = payload.get("agents")
    if isinstance(agents, str) and agents.strip():
        try:
            return _inherit_binaries(
                resolve_agents(parse_agents_argument(agents)), defaults
            )
        except CatalogError as exc:
            raise RegistryError(str(exc)) from exc

    entries = payload.get("panel")
    if entries is not None and not (isinstance(entries, list) and entries):
        # Falling through to council.yaml here meant `panel: []` — a form the user had
        # just cleared — quietly convened four real models and spent the subscription.
        # Only an absent `panel` may mean "use the default".
        raise RegistryError(
            "panel must be a non-empty list of panelists. Omit it entirely to use the "
            "panel from council.yaml."
        )
    if isinstance(entries, list) and entries:
        panel = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RegistryError(f"panel[{i}] must be an object")
            name = entry.get("name")
            adapter = entry.get("adapter")
            if not name or not adapter:
                raise RegistryError(f"panel[{i}] needs both 'name' and 'adapter'")
            panel.append(
                PanelistConfig(
                    name=str(name),
                    adapter=str(adapter),
                    model=entry.get("model") or None,
                    variant=entry.get("variant") or None,
                    binary=entry.get("binary") or None,
                )
            )
        names = [p.name for p in panel]
        if len(set(names)) != len(names):
            raise RegistryError("panelist names must be unique")
        if len(panel) < 2:
            raise RegistryError("a council needs at least 2 panelists")
        return _inherit_binaries(panel, defaults)

    if len(defaults.panel) < 2:
        raise RegistryError(
            "no panel given and council.yaml does not define one. "
            "Pass `panel` or `agents`."
        )
    return list(defaults.panel)


def _inherit_binaries(
    panel: list[PanelistConfig], defaults: CouncilConfig
) -> list[PanelistConfig]:
    """Fill in each panelist's harness path from council.yaml.

    Where a harness lives is a property of this machine, not of this council. An
    ad-hoc panel — edited in the form, or named with `--agents` — has no way to know
    it and no business asking, so it inherits. Without this, editing the panel in the
    UI silently dropped the path council.yaml gives codex and every panelist was
    dropped on its first turn as "executable not found".
    """
    known = {p.adapter: p.binary for p in defaults.panel if p.binary}
    if not known:
        return panel
    for entry in panel:
        if not entry.binary:
            entry.binary = known.get(entry.adapter)
    return panel


def _check_limits(protocol: ProtocolConfig, timeouts: TimeoutConfig) -> None:
    """The same floors council.yaml enforces, for requests that never go near it.

    `parse_config` validates the file; the API builds a config from a dict and so
    skipped all of it. A council with zero rounds explored the whole repository and
    then held no discussion, and one with a zero budget stopped after its first turn —
    both accepted, both paid for.
    """
    positive = (
        ("min_rounds", protocol.min_rounds),
        ("max_rounds", protocol.max_rounds),
        ("token_budget", protocol.token_budget),
        ("wall_clock_budget", protocol.wall_clock_budget),
        ("compaction_threshold", protocol.compaction_threshold),
        ("timeouts.per_call", timeouts.per_call),
        ("timeouts.per_call_phase1", timeouts.per_call_phase1),
    )
    for name, value in positive:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise RegistryError(f"{name} must be a whole number of at least 1, got {value!r}")
    # Clamped rather than refused: lowering only the ceiling is a reasonable request,
    # and the floor has no meaning above it.
    protocol.min_rounds = min(protocol.min_rounds, protocol.max_rounds)


def _section(cls, override: object, fallback):
    values = {f: getattr(fallback, f) for f in cls.__dataclass_fields__}
    if isinstance(override, dict):
        unknown = set(override) - set(values)
        if unknown:
            raise RegistryError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        values.update(override)
    try:
        return cls(**values)
    except TypeError as exc:
        raise RegistryError(str(exc)) from exc


def _first_line(path: Path, limit: int = 140) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    line = next((l for l in text.splitlines() if l.strip()), "")
    return line[:limit]


def _started_at(session_id: str) -> str | None:
    """When a session began, read back out of the id `new_id` minted for it.

    The id is the only record of the start time that every session has — status.json
    is rewritten as the run progresses and keeps no memory of its first line. Reading
    it back here beats writing a second copy that could disagree with the first.
    """
    stamp = session_id.split("-", 3)
    try:
        when = datetime.strptime("-".join(stamp[:3])[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None
    return when.astimezone().isoformat()


def _mode_of(path: Path) -> str:
    return "review" if (path / "seed.md").is_file() else "independent"


def _live_state(runtime) -> str | None:
    """What a session this daemon owns is really in, named as the detail view names it."""
    if runtime is None:
        return None
    if runtime.state in ("starting", "running"):
        return "paused" if runtime.controller.paused else "running"
    return runtime.state


def _resting_state(status: dict) -> str:
    """What a session nobody is running is really in.

    A session whose last word was "running" but whose process has gone — the daemon was
    restarted, or the machine was — was interrupted. Reporting it as live would be a
    lie the list never recovers from.
    """
    state = str(status.get("state") or "unknown")
    if state != "running":
        return state
    return "running" if pid_alive(status.get("pid")) else "interrupted"
