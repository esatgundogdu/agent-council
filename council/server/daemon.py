"""Daemon lifecycle: find the running one, or start a detached one.

`council up` is idempotent. It reads `~/.council/daemon.json`, asks whatever is on
that port whether it is a council, and reuses it if so — the port matters less than
the identity, because 8787 may well belong to something else entirely.

The daemon is started **detached**, not as a background job of whoever asked for it.
The UI has to outlive the agent turn that opened it and the Claude Code session after
that: the user should be able to close their editor, keep watching a forty-minute
debate, and start the next council from the browser.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .. import PACKAGE_ROOT, __version__
from .security import TOKEN_HEADER, new_token

STATE_DIR = Path.home() / ".council"
DAEMON_FILE = STATE_DIR / "daemon.json"
LOG_FILE = STATE_DIR / "daemon.log"

DEFAULT_PORT = 8787
START_TIMEOUT = 25.0

#: Windows creation flags. DETACHED_PROCESS gives the daemon no console and
#: CREATE_NEW_PROCESS_GROUP keeps Ctrl-C in the launching shell away from it.
_DETACH = 0x00000008 | 0x00000200

#: CREATE_BREAKAWAY_FROM_JOB. Terminals, IDEs and agent harnesses commonly run each
#: command inside a Job Object that kills everything in it when the command ends —
#: which would take the daemon with it, however detached it is. Breaking away is the
#: only way a control plane started from `/council` survives that turn.
_BREAKAWAY = 0x01000000


class DaemonError(Exception):
    """The daemon could not be reached or started."""


def read_daemon() -> dict:
    if not DAEMON_FILE.is_file():
        return {}
    try:
        data = json.loads(DAEMON_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_daemon(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DAEMON_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _restrict(DAEMON_FILE)


def _restrict(path: Path) -> None:
    """Keep the token to this user. Best effort — a failure here is not fatal."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.getlogin()}:F"],
                capture_output=True,
                timeout=15,
            )
        else:
            path.chmod(0o600)
    except (OSError, subprocess.SubprocessError):
        pass


def probe(port: int, token: str | None = None, timeout: float = 2.0) -> dict | None:
    """Ask whatever is on this port whether it is a council daemon."""
    headers = {TOKEN_HEADER: token} if token else {}
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/api/health", headers=headers, timeout=timeout
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) and payload.get("app") == "council" else None


def free_port(preferred: int) -> int:
    with socket.socket() as probe_socket:
        try:
            probe_socket.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return probe_socket.getsockname()[1]


def current() -> dict | None:
    """The running daemon, if `daemon.json` still describes a live one."""
    record = read_daemon()
    port, token = record.get("port"), record.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return None
    health = probe(port, token)
    if health is None:
        return None
    record["health"] = health
    return record


def ensure_running(
    port: int = DEFAULT_PORT, wait: float = START_TIMEOUT, idle_seconds: float = 0.0
) -> dict:
    """The daemon's connection details, starting it if nothing is listening.

    `idle_seconds` only applies to a daemon this call starts. One already running
    keeps whatever lifetime it was given — a shortcut must not quietly put an
    expiry on the daemon somebody else is using.
    """
    existing = current()
    if existing is not None:
        return existing

    # Reuse the previous token rather than rotating it. It is a local secret in a
    # 0600 file, and rotating it on every restart would silently break every browser
    # tab the user has open on the control plane.
    previous = read_daemon().get("token")
    token = previous if isinstance(previous, str) and previous else new_token()
    chosen = free_port(port)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _spawn(chosen, token, idle_seconds)

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        health = probe(chosen, token, timeout=1.0)
        if health is not None:
            record = {
                "app": "council",
                "version": __version__,
                "pid": health.get("pid"),
                "port": chosen,
                "token": token,
                "started_at": time.time(),
            }
            write_daemon(record)
            record["health"] = health
            return record
        time.sleep(0.25)

    tail = ""
    if LOG_FILE.is_file():
        tail = "\n".join(
            LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        )
    raise DaemonError(
        f"the daemon did not come up on port {chosen} within {wait:.0f}s."
        + (f"\n\nLast lines of {LOG_FILE}:\n{tail}" if tail else "")
    )


def _spawn(port: int, token: str, idle_seconds: float = 0.0) -> None:
    daemon_argv = [
        sys.executable, "-m", "council.server",
        "--port", str(port), "--token", token,
    ]
    if idle_seconds > 0:
        daemon_argv += ["--idle-seconds", str(idle_seconds)]
    # Via the launcher, which exits at once: see council/server/launcher.py for why
    # spawning the daemon directly is not enough to make it outlive this shell.
    argv = [
        sys.executable, "-m", "council.server.launcher",
        "--log", str(LOG_FILE), "--", *daemon_argv,
    ]
    log = LOG_FILE.open("a", encoding="utf-8")
    log.write(f"\n--- starting on port {port} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.flush()
    # The daemon runs from the home directory — it is not tied to any one project —
    # so a source checkout that was never `pip install`ed still has to be importable.
    env = dict(os.environ)
    root = str(PACKAGE_ROOT)
    if root not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [root, env.get("PYTHONPATH", "")]))

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "cwd": str(Path.home()),
        "close_fds": True,
        "env": env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
        try:
            subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise DaemonError(f"could not start the daemon: {exc}") from exc
        return

    try:
        subprocess.Popen(argv, creationflags=_DETACH | _BREAKAWAY, **kwargs)
    except OSError:
        # The job forbids breakaway. The daemon still starts; it just cannot outlive
        # the shell that spawned it, which is worth saying out loud when it happens.
        try:
            subprocess.Popen(argv, creationflags=_DETACH, **kwargs)
        except OSError as exc:
            raise DaemonError(f"could not start the daemon: {exc}") from exc
        log.write(
            "warning: could not break away from the parent job object; this daemon "
            "will exit when its parent shell does.\n"
        )
        log.flush()


def stop() -> bool:
    """Ask the running daemon to exit. True if one was there to stop."""
    record = current()
    if record is None:
        DAEMON_FILE.unlink(missing_ok=True)
        return False
    pid = record.get("pid") or record.get("health", {}).get("pid")
    if not isinstance(pid, int):
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=20,
            )
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    DAEMON_FILE.unlink(missing_ok=True)
    return True


def url(record: dict, with_token: bool = False) -> str:
    base = f"http://127.0.0.1:{record['port']}/"
    return f"{base}?token={record['token']}" if with_token else base


def serve(
    port: int,
    token: str,
    host: str = "127.0.0.1",
    dev_origin: str = "",
    idle_seconds: float = 0.0,
) -> None:
    """Run the daemon in this process (what the detached child does).

    `uvicorn.run` is a one-liner that keeps its `Server` to itself, and the server is
    the only thing that can be asked to stop. So it is built by hand here: `should_exit`
    is checked on every tick of the main loop, which makes it a graceful shutdown rather
    than a process killed from the outside with a request half-written.
    """
    import uvicorn

    from .app import create_app
    from .idle import Idle

    extra = (dev_origin,) if dev_origin else ()
    idle = Idle(idle_seconds, busy=lambda: False) if idle_seconds > 0 else None
    app = create_app(token=token, extra_origins=extra, idle=idle)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    if idle is not None:
        idle.stop = lambda: setattr(server, "should_exit", True)
    server.run()
