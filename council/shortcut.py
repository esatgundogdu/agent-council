"""A desktop shortcut that is the whole interface: open it, use it, close the tab.

The daemon it starts is given `--exit-when-idle`, so the pair behave like one thing —
double-click and the control plane is there, close the last tab and a minute and a half
later it is gone. No terminal to keep open, and nothing to remember to shut down.

On Windows the shortcut is a real `.lnk`, written through the same COM object Explorer
uses, because a `.cmd` would flash a console window every time. Elsewhere it is a
`.desktop` entry, which is what the freedesktop spec asks for and what every Linux menu
reads. macOS has no equivalent single-file launcher worth writing, so it is told what to
do instead of being given something that half works.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Written with `pythonw` where there is one: `python.exe` would flash a console for the
#: fraction of a second `council up` takes, on every launch, forever.
_QUIET_INTERPRETERS = ("pythonw.exe", "pythonw")


class ShortcutError(Exception):
    """The shortcut could not be written."""


def interpreter() -> str:
    """The console-less interpreter next to this one, or this one."""
    here = Path(sys.executable)
    for name in _QUIET_INTERPRETERS:
        candidate = here.with_name(name)
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def desktop() -> Path:
    """Where the user's desktop is, asking the system rather than assuming English."""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            path = Path(out.stdout.strip())
            if out.returncode == 0 and path.is_dir():
                return path
        except (OSError, subprocess.SubprocessError):
            pass
    # XDG names the desktop directory, and it is not always "Desktop".
    config = Path.home() / ".config" / "user-dirs.dirs"
    if config.is_file():
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("XDG_DESKTOP_DIR"):
                raw = line.partition("=")[2].strip().strip('"')
                path = Path(raw.replace("$HOME", str(Path.home())))
                if path.is_dir():
                    return path
    return Path.home() / "Desktop"


def arguments(idle_seconds: float, port: int | None = None) -> list[str]:
    argv = ["-m", "council", "up", "--app", "--exit-when-idle", str(idle_seconds)]
    if port:
        argv += ["--port", str(port)]
    return argv


def create(idle_seconds: float, port: int | None = None, into: Path | None = None) -> Path:
    """Write the shortcut and return where it landed."""
    folder = into or desktop()
    folder.mkdir(parents=True, exist_ok=True)
    argv = arguments(idle_seconds, port)
    if os.name == "nt":
        return _windows(folder / "Council.lnk", argv)
    return _freedesktop(folder / "council.desktop", argv)


def _windows(path: Path, argv: list[str]) -> Path:
    # WScript.Shell is how Explorer itself writes these; there is no file format to get
    # wrong, and no dependency to install. Quoted per-argument so a path with a space in
    # it — which `sys.executable` very often is — survives.
    quoted = " ".join(f'"{a}"' if " " in a else a for a in argv)
    script = f"""
$link = (New-Object -ComObject WScript.Shell).CreateShortcut('{path}')
$link.TargetPath = '{interpreter()}'
$link.Arguments = '{quoted}'
$link.WorkingDirectory = '{Path.home()}'
$link.Description = 'Plan Council — opens the control plane, and closes it when you are done'
$link.Save()
"""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShortcutError(f"could not run PowerShell to write the shortcut: {exc}") from exc
    if done.returncode != 0 or not path.is_file():
        raise ShortcutError(done.stderr.strip() or f"the shortcut was not written to {path}")
    return path


def _freedesktop(path: Path, argv: list[str]) -> Path:
    command = " ".join(f'"{a}"' if " " in a else a for a in [interpreter(), *argv])
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Plan Council\n"
        "Comment=Opens the control plane, and closes it when you are done\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "Categories=Development;\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path
