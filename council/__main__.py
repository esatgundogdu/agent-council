"""CLI entry point: `python -m council run --task <file>`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from . import DEFAULT_CONFIG_PATH, OPENCODE_CONFIG_PATH, PROMPTS_DIR
from .adapters import AdapterError
from .catalog import (
    CatalogError,
    describe_catalog,
    parse_agents_argument,
    resolve_agents,
)
from .config import ConfigError, CouncilConfig, PanelistConfig, load_config
from .orchestrator import Council, CouncilError, SessionPaths
from .panel import build_panel

EXIT_OK = 0
EXIT_PANEL = 2
EXIT_CONFIG = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council",
        description="Mature a plan by having several model agents debate it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a council session")
    run.add_argument("--task", required=True, help="file holding the task description")
    run.add_argument("--config", default=None, help=f"council.yaml (default: {DEFAULT_CONFIG_PATH})")
    run.add_argument(
        "--project-dir",
        default=".",
        help="repository the panel explores and where .council/ is written",
    )
    run.add_argument("--session-dir", default=None, help="explicit output directory")
    run.add_argument(
        "--agents",
        default=None,
        metavar="LIST",
        help="comma-separated panel, overriding council.yaml, e.g. "
        "\"gpt, glm-5.2, kimi k2.6, claude opus 4.8\". See `council models`.",
    )
    run.add_argument(
        "--mock",
        action="store_true",
        help="replace the panel with scripted mock agents (no model calls)",
    )
    run.add_argument("--scenario", default=None, help="JSON scenario for --mock")
    run.add_argument("--max-rounds", type=int, default=None, help="override max_rounds")
    run.add_argument("--quiet", action="store_true", help="suppress progress output")

    sub.add_parser("models", help="list the agents you can pass to --agents")

    watch = sub.add_parser("serve", help="follow a council session in the browser")
    watch.add_argument(
        "--project-dir", default=".", help="repo whose .council/ to watch (default .)"
    )
    watch.add_argument(
        "--session",
        default=None,
        help="a specific session directory (default: newest, and it follows new runs)",
    )
    watch.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    watch.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    return parser


def _mock_config(config: CouncilConfig) -> CouncilConfig:
    config.panel = [
        PanelistConfig(name=p.name, adapter="mock", model=p.model, focus=p.focus)
        for p in config.panel
    ] or [
        PanelistConfig(name="alpha", adapter="mock"),
        PanelistConfig(name="beta", adapter="mock"),
    ]
    return config


def _session_dir(project_dir: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return project_dir / ".council" / stamp


def cmd_run(args) -> int:
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: --project-dir is not a directory: {project_dir}", file=sys.stderr)
        return EXIT_CONFIG

    task_src = Path(args.task).expanduser().resolve()
    if not task_src.is_file():
        print(f"error: task file not found: {task_src}", file=sys.stderr)
        return EXIT_CONFIG

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

    panel = build_panel(config)
    progress = (lambda msg: None) if args.quiet else _printer()

    council = Council(
        config=config,
        panel=panel,
        paths=paths,
        project_dir=project_dir,
        prompts_dir=PROMPTS_DIR,
        opencode_config=OPENCODE_CONFIG_PATH if OPENCODE_CONFIG_PATH.is_file() else None,
        scenario_path=args.scenario,
        progress=progress,
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


def _printer():
    def emit(msg: str) -> None:
        print(msg, flush=True)

    return emit


def cmd_models(_args) -> int:
    print(describe_catalog())
    return EXIT_OK


def cmd_serve(args) -> int:
    from .dashboard import find_sessions, serve

    project_dir = Path(args.project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return EXIT_CONFIG

    session = None
    if args.session:
        session = Path(args.session).expanduser().resolve()
        if not session.is_dir():
            # Also accept a bare session name, e.g. `--session 2026-07-25_182639`.
            match = [p for p in find_sessions(project_dir) if p.name == args.session]
            if not match:
                print(f"error: no such session: {args.session}", file=sys.stderr)
                return EXIT_CONFIG
            session = match[0]

    serve(
        project_dir=project_dir,
        session=session,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "models":
        return cmd_models(args)
    if args.command == "serve":
        return cmd_serve(args)
    return EXIT_CONFIG  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())
