"""CLI entry point.

Two ways to convene a council, and they are not the same thing:

* ``council start`` hands the work to the **daemon**, which is what the web UI drives
  too. This is the normal path, and the one the `/council` slash command uses: the
  session is visible in the browser, controllable while it runs, and it outlives the
  shell that started it.
* ``council run`` does the whole thing **in this process**, with no daemon and no
  port. Headless, scriptable, and what the mock end-to-end test exercises.

Everything else here — `up`, `watch`, `status`, `digest` — is a thin client of the
daemon's HTTP API, which is the same API the browser uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import DEFAULT_CONFIG_PATH, OPENCODE_CONFIG_PATH, PROMPTS_DIR, __version__
from .adapters import AdapterError
from .catalog import (
    CatalogError,
    configured_binaries,
    describe_catalog,
    parse_agents_argument,
    resolve_agents,
)
from .client import Client
from .config import ConfigError, CouncilConfig, PanelistConfig, load_config
from .orchestrator import (
    ACCEPTS_SEED,
    CONVENERS,
    MODES,
    SEEDED_MODES,
    TERMINATION_LABELS,
    Council,
    CouncilError,
    SessionPaths,
)
from .panel import build_panel
from .server.daemon import DEFAULT_PORT, DaemonError

EXIT_OK = 0
EXIT_PANEL = 2
EXIT_CONFIG = 3
# `wait --timeout` ran out with the council still arguing. Distinct from every other
# code because the caller's next move is different: not "it broke", but "ask me again".
EXIT_TIMEOUT = 4

#: Default quiet before an `--exit-when-idle` daemon lets go. Long enough that a
#: browser still opening, or a tab being reloaded, is never mistaken for going away.
IDLE_SECONDS = 90.0

# How long to leave a dropped event stream alone before picking it up again.
RECONNECT_SECONDS = 2.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council",
        description="Mature a plan by having several model agents debate it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- the daemon ----
    up = sub.add_parser("up", help="start the control plane (idempotent) and print its URL")
    up.add_argument("--port", type=int, default=DEFAULT_PORT)
    up.add_argument("--open", action="store_true", help="also open a browser")
    up.add_argument(
        "--exit-when-idle",
        dest="idle_seconds",
        nargs="?",
        type=float,
        const=IDLE_SECONDS,
        default=0.0,
        metavar="SECONDS",
        help=f"shut the daemon down once no tab is open and nothing is running "
        f"(default {IDLE_SECONDS:.0f}s of quiet). Only applies to a daemon this "
        f"command starts; it will never exit while a council is running.",
    )
    up.add_argument(
        "--foreground",
        action="store_true",
        help="run it in this terminal instead of in the background (Ctrl+C to stop)",
    )
    up.add_argument(
        "--dev-origin",
        default="",
        help="extra allowed Origin, e.g. http://localhost:5173 for the Vite dev server",
    )

    sub.add_parser("down", help="stop the control plane")

    link = sub.add_parser(
        "shortcut",
        help="put a desktop shortcut that opens the control plane and closes it after",
    )
    link.add_argument("--port", type=int, default=None)
    link.add_argument(
        "--idle-seconds",
        type=float,
        default=IDLE_SECONDS,
        help=f"quiet before the daemon lets go (default {IDLE_SECONDS:.0f})",
    )
    link.add_argument("--into", default=None, help="write it here instead of the desktop")

    # ---- sessions through the daemon ----
    start = sub.add_parser("start", help="convene a council through the control plane")
    _session_args(start)
    start.add_argument("--json", action="store_true", help="print the created session as JSON")
    start.add_argument("--follow", action="store_true", help="stream progress until it ends")
    start.add_argument(
        "--mock",
        action="store_true",
        help="scripted panel, no model calls — for trying the UI out for free",
    )
    start.add_argument("--scenario", default=None, help="JSON scenario for --mock")
    start.add_argument(
        "--by",
        default="user",
        choices=list(CONVENERS),
        help="who set the task. Only names the seat at the head of the table in the UI; "
        "it grants nothing. `/council` passes `agent`.",
    )

    watch = sub.add_parser("watch", help="follow a running session in this terminal")
    watch.add_argument("session", nargs="?", help="session id (default: the newest)")

    wait = sub.add_parser(
        "wait",
        help="block until a session ends, quietly; exit 0 done, 2 failed, 4 timed out",
    )
    wait.add_argument("session", nargs="?", help="session id (default: the newest)")
    wait.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="give up waiting after this long and exit 4. 0 (default) waits forever.",
    )

    status = sub.add_parser("status", help="one-shot progress report")
    status.add_argument("session", nargs="?", help="session id (default: the newest)")
    status.add_argument("--json", action="store_true")

    digest = sub.add_parser("digest", help="print a finished session's digest")
    digest.add_argument("session", nargs="?", help="session id (default: the newest)")
    digest.add_argument("--path", action="store_true", help="print the file path instead")

    sessions = sub.add_parser("sessions", help="list sessions across every project")
    sessions.add_argument("--project", default=None)
    sessions.add_argument("--json", action="store_true")

    control = sub.add_parser("control", help="steer a running session")
    control.add_argument("action", choices=[
        "pause", "resume", "stop", "skip", "drop", "restore", "extend", "digest", "chair",
    ])
    control.add_argument("session", nargs="?", help="session id (default: the newest)")
    control.add_argument("--agent", default=None, help="for skip/drop/restore")
    control.add_argument("--text", default=None, help="for chair")
    control.add_argument("--how", default="graceful", choices=["graceful", "hard"])
    control.add_argument("--max-rounds", type=int, default=None, help="for extend")

    # ---- headless ----
    run = sub.add_parser("run", help="run a council in this process, without the daemon")
    _session_args(run, headless=True)
    run.add_argument("--config", default=None, help=f"council.yaml (default: {DEFAULT_CONFIG_PATH})")
    run.add_argument("--session-dir", default=None, help="explicit output directory")
    run.add_argument("--mock", action="store_true", help="scripted panel, no model calls")
    run.add_argument("--scenario", default=None, help="JSON scenario for --mock")
    run.add_argument(
        "--max-rounds", type=_positive, default=None, help="override max_rounds"
    )
    run.add_argument("--quiet", action="store_true", help="suppress progress output")

    models = sub.add_parser("models", help="list the agents you can pass to --agents")
    models.add_argument("--config", help="council.yaml to read binary overrides from")
    return parser


def _session_args(parser: argparse.ArgumentParser, headless: bool = False) -> None:
    parser.add_argument("--task", required=True, help="file holding the task description")
    parser.add_argument(
        "--project-dir", default=".", help="repository the panel explores (default .)"
    )
    parser.add_argument(
        "--mode",
        default="independent",
        choices=list(MODES),
        help="what the panel starts from. independent: plans it writes itself; review: "
        "your --seed; hybrid: both, seeing --seed only after it has planned; consult: "
        "your --context, answered in parallel in round 1 and debated after that. All "
        "four then argue for up to --max-rounds rounds.",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="file holding the proposal to review ('-' reads stdin)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="file holding a brief on where the work already stands ('-' reads stdin). "
        "Never shown to a panelist writing its independent plan.",
    )
    parser.add_argument(
        "--agents",
        default=None,
        metavar="LIST",
        help="panel for this run, e.g. \"gpt, glm-5.2, claude opus\". See `council models`.",
    )
    if not headless:
        parser.add_argument("--max-rounds", type=_positive, default=None)


def _positive(value: str) -> int:
    """A round count of zero explored the whole repository and then held no rounds.

    It was accepted, produced a digest saying every panelist "never completed a turn",
    and exited 0 — after paying for Phase 1.
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got '{value}'") from None
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"must be at least 1, got {number} — a council with no rounds explores the "
            "repository and then holds no discussion"
        )
    return number


# ---- the daemon ---------------------------------------------------------


def cmd_up(args) -> int:
    from .server import daemon

    if args.foreground:
        return _serve_here(daemon, args)
    try:
        record = daemon.ensure_running(args.port, idle_seconds=args.idle_seconds)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    link = daemon.url(record, with_token=True)
    print(f"Council control plane: {link}")
    if args.open:
        import webbrowser

        webbrowser.open(link)
    return EXIT_OK


def cmd_shortcut(args) -> int:
    """One icon: opens the control plane, and takes it away when you are done."""
    from .shortcut import ShortcutError, create

    if sys.platform == "darwin":
        print(
            "macOS has no single-file launcher worth writing. Make one in Automator, "
            "or add this line to your shell profile:\n\n"
            f"  alias council-open='{sys.executable} -m council up --open "
            f"--exit-when-idle {args.idle_seconds:.0f}'",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        path = create(
            args.idle_seconds,
            args.port,
            Path(args.into).expanduser() if args.into else None,
        )
    except ShortcutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"Shortcut: {path}")
    print(
        "Open it and the control plane starts and opens in your browser. Close the\n"
        f"last tab and it shuts down after {args.idle_seconds:.0f}s of quiet — never\n"
        "while a council is still running."
    )
    return EXIT_OK


def _serve_here(daemon, args) -> int:
    """Run the daemon attached, and still register it.

    This was `council serve`, which never called `write_daemon` — so it served a
    working UI while `council status` reported no daemon running and `council down`
    said there was nothing to stop.
    """
    from .server.security import new_token

    if daemon.current() is not None:
        print(
            "error: a daemon is already running. Stop it first with `council down`.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    token = new_token()
    port = daemon.free_port(args.port)
    record = {"port": port, "token": token, "pid": os.getpid(), "version": __version__}
    daemon.write_daemon(record)
    print(f"Council control plane: {daemon.url(record, with_token=True)}")
    print("Ctrl+C to stop.")
    try:
        daemon.serve(port=port, token=token, dev_origin=args.dev_origin)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        _forget(daemon)
    return EXIT_OK


def _forget(daemon) -> None:
    """Remove the daemon record on the way out, so nothing advertises a dead port."""
    try:
        daemon.DAEMON_FILE.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - nothing useful to do about it
        pass


def cmd_down(_args) -> int:
    from .server import daemon

    print("stopped" if daemon.stop() else "nothing was running")
    return EXIT_OK




# ---- sessions through the daemon ----------------------------------------


def cmd_start(args) -> int:
    project_dir = Path(args.project_dir).expanduser().resolve()
    task = _read_task(args.task)
    if task is None:
        return EXIT_CONFIG
    inputs = _read_inputs(args)
    if inputs is None:
        return EXIT_CONFIG
    seed, context = inputs

    payload = {
        "project_dir": str(project_dir),
        "register_project": True,
        "task": task,
        "mode": args.mode,
        "convened_by": args.by,
    }
    if seed:
        payload["seed"] = seed
    if context:
        payload["context"] = context
    if args.agents:
        payload["agents"] = args.agents
    if args.max_rounds is not None:
        # Only the ceiling. This used to pin min_rounds to 1 as well, so a unanimous
        # first round ended the council — exactly what min_rounds exists to prevent —
        # silently, on a flag that says nothing about it. The daemon clamps the floor
        # to the ceiling where they conflict.
        payload["protocol"] = {"max_rounds": args.max_rounds}
    if args.mock:
        payload["panel"] = [
            {"name": "alpha", "adapter": "mock"},
            {"name": "beta", "adapter": "mock"},
            {"name": "gamma", "adapter": "mock"},
        ]
        payload["scenario"] = (
            str(Path(args.scenario).expanduser().resolve()) if args.scenario else None
        )

    try:
        client = Client.connect()
        created = client.create(payload)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    created["url"] = f"{client.url}session/{created['id']}"
    if args.json:
        print(json.dumps(created, indent=2))
    else:
        panel = ", ".join(p["name"] for p in created["panel"])
        print(f"Session {created['id']} ({created['mode']}): {panel}")
        print(f"Watch it: {created['url']}")
    return _follow(client, created["id"]) if args.follow else EXIT_OK


def cmd_watch(args) -> int:
    try:
        client = Client.connect(start=False)
        session_id = _resolve(client, args.session)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return _follow(client, session_id)


def cmd_wait(args) -> int:
    """Block until a session is over, and say which way it went in the exit code.

    `watch` is for a person reading a terminal. This is for a program: one line out, and
    an exit code another process can branch on — which is what lets an agent hand the
    waiting to its own job control and be woken when the council lands, instead of
    sleeping and polling and hoping it asks again at the right moment.
    """
    try:
        client = Client.connect(start=False)
        session_id = _resolve(client, args.session)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return _await(client, session_id, describe=False, deadline=args.timeout)


def _follow(client: Client, session_id: str) -> int:
    """Turn the event stream into the same progress lines a local run prints."""
    return _await(client, session_id, describe=True, deadline=0.0)


def _await(client: Client, session_id: str, describe: bool, deadline: float) -> int:
    """Follow a session to its end, surviving a stream that drops on the way.

    The generator ending is not proof the session ended: a daemon restart, a proxy timing
    out and a laptop lid closing all end it exactly the same way. So the stream stopping
    only prompts the question, and the session's own state answers it. Reconnecting is
    free of duplicates because the log is append-only and the server replays `from_seq`.

    This matters much more here than it reads. As long as a person is watching the lines
    go by, a stream that quietly gives up costs them a re-run. The moment the exit is a
    signal — an agent waiting to be woken — the same bug reports a council as finished
    while it is still arguing, and the digest that gets read is the one that is not
    written yet.
    """
    mode = ""
    seq = 0
    started = time.monotonic()
    while True:
        try:
            for record in client.events(session_id, from_seq=seq):
                seq = max(seq, int(record.get("seq") or 0))
                if record.get("event") == "session_created":
                    mode = str(record.get("mode") or "")
                if describe:
                    line = _describe(record, mode)
                    if line:
                        print(line, flush=True)
        except DaemonError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        except KeyboardInterrupt:
            print("\n(detached — the session keeps running)", file=sys.stderr)
            return EXIT_OK

        try:
            state = client.session(session_id)["status"]
        except DaemonError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CONFIG

        if state["state"] == "failed":
            print(f"\ncouncil failed: {state.get('error')}", file=sys.stderr)
            return EXIT_PANEL
        if state["state"] != "running":
            digest = Path(client.session(session_id)["session"]["dir"]) / "digest.md"
            print(f"\nDIGEST: {digest}" if describe else f"DIGEST: {digest}")
            return EXIT_OK

        # Still running, so the stream dropped rather than ended. Pick it up where it
        # left off — but not in a hot loop, and not forever if the caller set a limit.
        if deadline and time.monotonic() - started > deadline:
            print(
                f"still running after {deadline:.0f}s: {session_id}",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT
        time.sleep(RECONNECT_SECONDS)


def _describe(record: dict, mode: str = "") -> str:
    kind = record.get("event")
    agent = record.get("agent", "")
    if kind == "session_created":
        return f"Session {record.get('id')} · mode {record.get('mode')}"
    if kind == "phase_start":
        # A consultation has no Phase 1, so numbering its rounds "Phase 2" names an
        # implementation detail rather than anything happening in the user's session.
        if mode == "consult":
            return "The discussion" if record.get("phase") == 2 else "Writing the digest"
        return f"Phase {record.get('phase')}"
    if kind == "round_start":
        head = f"  Round {record.get('round')}:"
        if mode == "consult" and record.get("round") == 1:
            return f"{head} everyone at once, nobody seeing the others"
        return head
    if kind == "plan_received":
        return f"  + {agent} plan ready ({record.get('chars')} chars, {record.get('seconds')}s)"
    if kind == "turn_end":
        return f"    {agent}: {record.get('verdict')} ({record.get('seconds')}s)"
    if kind == "turn_failed":
        return f"    ! {agent} failed: {str(record.get('error'))[:120]}"
    if kind == "panelist_dropped":
        return f"  ! {agent} dropped: {str(record.get('reason'))[:120]}"
    if kind == "chair_message":
        return f"  · chair: {str(record.get('text'))[:100]}"
    if kind == "control":
        return f"  · control: {record.get('action')}"
    if kind == "terminated":
        reason = str(record.get("reason"))
        return f"  ended: {TERMINATION_LABELS.get(reason, reason)}"
    if kind == "session_end":
        return f"Done · {record.get('rounds')} round(s) · ~{record.get('tokens')} tokens"
    if kind == "session_failed":
        return f"FAILED: {record.get('error')}"
    return ""


def cmd_status(args) -> int:
    try:
        client = Client.connect(start=False)
        state = client.session(_resolve(client, args.session))
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.json:
        print(json.dumps({"session": state["session"], "status": state["status"],
                          "panel": state["panel"]}, indent=2))
        return EXIT_OK

    status, session = state["status"], state["session"]
    print(f"{session['id']} · {session['mode']} · {status['state']}")
    print(
        f"  phase {status.get('phase')} · round {status.get('round')} · "
        f"{status.get('tokens') or 0} tokens · {status.get('elapsed') or 0}s"
    )
    for member in state["panel"]:
        mark = "dropped" if member["dropped"] else (member["verdict"] or "—")
        speaking = " (speaking now)" if member.get("speaking") else ""
        print(f"  {member['label']:<9} {mark}{speaking}")
    if state["has_digest"]:
        print(f"  digest: {Path(session['dir']) / 'digest.md'}")
    return EXIT_OK


def cmd_digest(args) -> int:
    try:
        client = Client.connect(start=False)
        session_id = _resolve(client, args.session)
        if args.path:
            print(Path(client.session(session_id)["session"]["dir"]) / "digest.md")
            return EXIT_OK
        print(client.digest(session_id))
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK


def cmd_sessions(args) -> int:
    try:
        rows = Client.connect(start=False).sessions(args.project)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("no sessions yet")
        return EXIT_OK
    for row in rows:
        live = " ●" if row["live"] else "  "
        print(f"{live} {row['id']}  {row['state']:<11} {row['mode']:<12} "
              f"{row['project']:<18} {row['task'][:50]}")
    return EXIT_OK


def cmd_control(args) -> int:
    payload: dict = {}
    if args.action in ("skip", "drop", "restore"):
        if not args.agent:
            print(f"error: --agent is required for {args.action}", file=sys.stderr)
            return EXIT_CONFIG
        payload["agent"] = args.agent
    if args.action == "chair":
        if not args.text:
            print("error: --text is required for chair", file=sys.stderr)
            return EXIT_CONFIG
        payload["text"] = args.text
        payload["author"] = "agent"
        # `author` names who the panel is told is speaking; `by` names who issued the
        # command. Only the first was being sent, so the control log recorded every
        # agent chair message as the user's — one command described two different
        # ways in the same session.
        payload["by"] = "agent"
    if args.action == "stop":
        payload["how"] = args.how
    if args.action == "extend":
        if args.max_rounds is None:
            print("error: --max-rounds is required for extend", file=sys.stderr)
            return EXIT_CONFIG
        payload["max_rounds"] = args.max_rounds

    try:
        client = Client.connect(start=False)
        record = client.control(_resolve(client, args.session), args.action, **payload)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{record['action']}: {record.get('detail')}")
    return EXIT_OK


def _resolve(client: Client, session_id: str | None) -> str:
    if session_id:
        return session_id
    rows = client.sessions()
    if not rows:
        raise DaemonError("no sessions yet")
    return rows[0]["id"]


# ---- headless -----------------------------------------------------------


def _mock_config(config: CouncilConfig) -> CouncilConfig:
    config.panel = [
        PanelistConfig(name=p.name, adapter="mock", model=p.model)
        for p in config.panel
    ] or [
        PanelistConfig(name="alpha", adapter="mock"),
        PanelistConfig(name="beta", adapter="mock"),
    ]
    return config


def _session_dir(project_dir: Path, explicit: str | None) -> Path:
    """A directory this council can own outright.

    The id is a whole-second timestamp, so two councils started in the same second
    used to land in one directory: one events.jsonl with two `session_created`
    records interleaved, and whichever finished last overwriting the digest. The
    daemon's own `new_id` already guards against this; the headless path did not.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    base = project_dir / ".council"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    candidate, n = base / stamp, 1
    while candidate.exists():
        n += 1
        candidate = base / f"{stamp}-{n}"
    return candidate


def cmd_run(args) -> int:
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: --project-dir is not a directory: {project_dir}", file=sys.stderr)
        return EXIT_CONFIG

    # Read it here, not only in the orchestrator: a task that is not UTF-8 must fail
    # before a session directory exists, not after Phase 1 has been paid for.
    task_src = Path(args.task).expanduser().resolve()
    if _read_task(args.task) is None:
        return EXIT_CONFIG

    inputs = _read_inputs(args)
    if inputs is None:
        return EXIT_CONFIG
    seed, context = inputs

    try:
        config = load_config(args.config or DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.agents:
        try:
            config.panel = resolve_agents(parse_agents_argument(args.agents))
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        # A compactor named in council.yaml need not be on an ad-hoc panel.
        chosen = {p.name for p in config.panel}
        if config.protocol.compaction_panelist not in chosen:
            config.protocol.compaction_panelist = None

    if args.mock:
        config = _mock_config(config)
    if args.max_rounds is not None:
        config.protocol.max_rounds = args.max_rounds
        config.protocol.min_rounds = min(config.protocol.min_rounds, args.max_rounds)

    paths = SessionPaths(root=_session_dir(project_dir, args.session_dir))
    paths.prepare()

    # The task reaches the panel byte-for-byte: no summary, no commentary, no framing.
    if paths.task.resolve() != task_src:
        paths.task.write_bytes(task_src.read_bytes())
    if seed:
        paths.seed.write_text(seed, encoding="utf-8", newline="")
    if context:
        paths.context.write_text(context, encoding="utf-8", newline="")

    panel = build_panel(config)
    progress = (lambda msg: None) if args.quiet else print

    council = Council(
        config=config,
        panel=panel,
        paths=paths,
        project_dir=project_dir,
        prompts_dir=PROMPTS_DIR,
        opencode_config=OPENCODE_CONFIG_PATH if OPENCODE_CONFIG_PATH.is_file() else None,
        scenario_path=args.scenario,
        progress=progress,
        mode=args.mode,
    )

    progress(f"Council session: {paths.root}")
    try:
        result = asyncio.run(council.run())
    except CouncilError as exc:
        print(f"\ncouncil failed: {exc}", file=sys.stderr)
        print(f"partial output in {paths.root}", file=sys.stderr)
        return EXIT_PANEL
    except AdapterError as exc:
        print(f"\nadapter error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_PANEL

    progress(
        f"\nDone in {result.duration / 60:.1f} min · {result.rounds} round(s) · "
        f"~{result.tokens} tokens"
    )
    print(f"\nDIGEST: {paths.digest}")
    return EXIT_OK



def cmd_models(args) -> int:
    # Through council.yaml's own binaries, so this lists what a council here would
    # really run — a harness the config points at off-PATH included.
    print(describe_catalog(_binaries(getattr(args, "config", None))))
    return EXIT_OK


def _binaries(config_path) -> dict[str, str]:
    try:
        return configured_binaries(load_config(config_path or DEFAULT_CONFIG_PATH).panel)
    except ConfigError:
        return {}


# ---- shared -------------------------------------------------------------


def read_text_file(path: Path, what: str) -> str | None:
    """A file the user named, or None with an error already printed.

    `utf-8-sig` because Notepad writes a byte-order mark by default, and that mark
    would otherwise be the first character of every panelist's task prompt. A file
    that is not UTF-8 at all gets a message naming the likely cause instead of a
    UnicodeDecodeError traceback — on a Turkish Windows, "save as ANSI" is one menu
    click away and produces exactly that.
    """
    if not path.is_file():
        print(f"error: {what} file not found: {path}", file=sys.stderr)
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        print(
            f"error: {what} file is not UTF-8: {path}. If you saved it from Notepad, "
            'choose "UTF-8" rather than ANSI or Unicode in the Save-as dialog.',
            file=sys.stderr,
        )
        return None
    except OSError as exc:
        print(f"error: cannot read {what} file {path}: {exc}", file=sys.stderr)
        return None


def _read_task(path: str) -> str | None:
    text = read_text_file(Path(path).expanduser().resolve(), "task")
    if text is not None and not text.strip():
        print(
            "error: the task file is empty. It is the only thing the panel is told, "
            "so there is nothing for it to plan.",
            file=sys.stderr,
        )
        return None
    return text


def _read_seed(args) -> str:
    """The proposal for review/hybrid/consult. '-' reads stdin, so an agent can pipe its own."""
    if not args.seed:
        if args.mode in SEEDED_MODES:
            print(
                f"error: mode '{args.mode}' needs --seed (a file, or '-' for stdin)",
                file=sys.stderr,
            )
            return None
        return ""
    if args.mode not in ACCEPTS_SEED:
        # Silently ignoring it wrote a seed.md the panel never saw — an artefact on
        # disk claiming a proposal was reviewed when no prompt ever contained it.
        print(
            f"error: --seed is not read by --mode {args.mode}. Modes that use it: "
            f"{', '.join(ACCEPTS_SEED)}.",
            file=sys.stderr,
        )
        return None
    if args.seed == "-":
        return sys.stdin.read()
    return read_text_file(Path(args.seed).expanduser().resolve(), "seed")


def _read_context(args) -> str:
    """The brief on where the work already stands. Valid in every mode.

    Unlike --seed this is never mode-checked, because it is never wrong to have: the
    orchestrator decides where it may appear, and the one place it may not is the
    independent-plan prompt.
    """
    if not args.context:
        return ""
    if args.context == "-":
        return sys.stdin.read()
    return read_text_file(Path(args.context).expanduser().resolve(), "context")


def _read_inputs(args) -> tuple[str, str] | None:
    """The proposal and the brief, or None with an error already printed.

    Together, because the one thing they can do to each other has to be caught before
    either of them reads: two `-` arguments would both take the same stream, whichever
    ran first would swallow all of it, and the second would see an empty file it had no
    reason to suspect.
    """
    if args.seed == "-" and args.context == "-":
        print(
            "error: --seed and --context cannot both read stdin — one of them would get "
            "the whole stream and the other nothing. Write one to a file first.",
            file=sys.stderr,
        )
        return None
    seed = _read_seed(args)
    if seed is None:
        return None
    context = _read_context(args)
    if context is None:
        return None
    return seed, context


COMMANDS = {
    "up": cmd_up,
    "down": cmd_down,
    "shortcut": cmd_shortcut,
    "start": cmd_start,
    "watch": cmd_watch,
    "wait": cmd_wait,
    "status": cmd_status,
    "digest": cmd_digest,
    "sessions": cmd_sessions,
    "control": cmd_control,
    "run": cmd_run,
    "models": cmd_models,
}


def _writable_console() -> None:
    """Make stdout able to carry this program's own prose.

    Every progress line, every help text and the whole model catalogue contain em
    dashes and typographic quotes. A Windows console is cp857 or cp1254 by default —
    Turkish machines, where this is developed — and Python then raises
    UnicodeEncodeError on `print`. That killed a council *after* Phase 1, having spent
    the money, because one status line said "min_rounds=3 — continuing".

    Replacement rather than an exception: a character that will not render is a
    cosmetic loss, and losing a paid session over one is not a trade worth making.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - a stream we cannot retune
            pass


def main(argv: list[str] | None = None) -> int:
    _writable_console()
    args = build_parser().parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the subcommand
        return EXIT_CONFIG
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
