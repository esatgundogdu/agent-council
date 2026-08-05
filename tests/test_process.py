"""The subprocess layer: starting a harness, timing it out, killing what it left.

These drive real processes rather than mocks, because the bugs they guard against are
exactly the ones a mock cannot reproduce: `os.killpg` and `signal.SIGKILL` do not exist
on Windows, and `CreateProcess` will not start the `.cmd` shims npm installs.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from council.adapters.base import AdapterError, resolve_binary, run_process

# Writes an incrementing counter to argv[1] forever. Bounded so that a test which
# fails to kill it cleans itself up within a minute rather than leaking a process.
HEARTBEAT = """
import pathlib, sys, time
target = pathlib.Path(sys.argv[1])
for i in range(1200):
    target.write_text(str(i))
    time.sleep(0.05)
"""

# Starts a heartbeat as its own child, then beats itself: a two-level process tree
# standing in for a harness that has a model client underneath it.
SPAWNER = """
import pathlib, subprocess, sys, time
heartbeat, out = sys.argv[1], pathlib.Path(sys.argv[2])
subprocess.Popen([sys.executable, heartbeat, str(out / "grandchild.txt")])
mine = out / "child.txt"
for i in range(1200):
    mine.write_text(str(i))
    time.sleep(0.05)
"""


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


def _is_frozen(path: Path, settle: float = 1.5) -> bool:
    """True if nothing writes to `path` any more — i.e. its writer is gone.

    Checked by content rather than by pid: on Windows a pid can still be openable
    while the process is dead, so a liveness probe would give a false positive.
    """
    before = path.read_text()
    time.sleep(settle)
    return path.read_text() == before


@pytest.fixture()
def tree(tmp_path: Path):
    """A running two-level process tree, already timed out and killed."""
    heartbeat = tmp_path / "heartbeat.py"
    heartbeat.write_text(HEARTBEAT, encoding="utf-8")
    spawner = tmp_path / "spawner.py"
    spawner.write_text(SPAWNER, encoding="utf-8")

    reply = asyncio.run(
        run_process(
            [sys.executable, str(spawner), str(heartbeat), str(tmp_path)],
            cwd=str(tmp_path),
            timeout=3,
        )
    )
    return reply, tmp_path


# ---- timeouts -----------------------------------------------------------


def test_timeout_is_reported_rather_than_raised(tree):
    # Regression: _kill_group used os.killpg/signal.SIGKILL, neither of which exists
    # on Windows, so every timeout died with AttributeError and took the run with it.
    reply, _ = tree
    assert reply.ok is False
    assert "timed out after 3s" in reply.error


def test_timeout_kills_the_process_it_started(tree):
    _, out = tree
    _wait_for(out / "child.txt")
    assert _is_frozen(out / "child.txt")


def test_timeout_kills_grandchildren_too(tree):
    # A hung harness holds a model client underneath it; killing only the process we
    # started leaves that one running and still spending tokens.
    _, out = tree
    _wait_for(out / "grandchild.txt")
    assert _is_frozen(out / "grandchild.txt")


def test_a_process_that_finishes_in_time_is_not_disturbed(tmp_path):
    reply = asyncio.run(
        run_process(
            [sys.executable, "-c", "print('done')"], cwd=str(tmp_path), timeout=30
        )
    )
    assert reply.ok is True
    assert reply.text.strip() == "done"
    assert reply.exit_code == 0


def test_a_failing_process_reports_its_stderr(tmp_path):
    reply = asyncio.run(
        run_process(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            cwd=str(tmp_path),
            timeout=30,
        )
    )
    assert reply.ok is False
    assert reply.exit_code == 3
    assert "boom" in reply.error


# ---- starting the harness ----------------------------------------------


def test_missing_binary_is_named_in_the_error(tmp_path):
    with pytest.raises(AdapterError) as caught:
        asyncio.run(
            run_process(["council-no-such-harness"], cwd=str(tmp_path), timeout=5)
        )
    assert "harness executable not found" in str(caught.value)
    assert "council-no-such-harness" in str(caught.value)


def _too_long_error() -> OSError:
    """The exception each platform raises for an over-long command line."""
    if os.name == "nt":
        # ERROR_FILENAME_EXCED_RANGE reaches Python as FileNotFoundError(errno=2),
        # which is why the winerror has to be inspected before the exception type.
        return OSError(errno.ENOENT, "too long", None, 206)
    return OSError(errno.E2BIG, "Argument list too long")


def test_over_long_command_line_is_not_blamed_on_a_missing_binary(
    tmp_path, monkeypatch
):
    async def refuse(*_args, **_kwargs):
        raise _too_long_error()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)

    with pytest.raises(AdapterError) as caught:
        asyncio.run(run_process([sys.executable, "x" * 50], cwd=str(tmp_path), timeout=5))

    message = str(caught.value)
    assert "command line too long" in message
    assert "compaction_threshold" in message
    assert "not found" not in message


# ---- locating the harness ----------------------------------------------


def test_absolute_paths_are_passed_through_untouched():
    assert resolve_binary(sys.executable) == sys.executable


def test_an_unresolvable_name_is_left_for_the_os_to_reject():
    # So the failure still surfaces as the usual "executable not found".
    assert resolve_binary("council-no-such-harness") == "council-no-such-harness"


def test_a_name_on_path_resolves_to_a_startable_file(tmp_path, monkeypatch):
    """The npm case: `codex` on PATH is `codex.cmd`, which CreateProcess cannot start.

    On POSIX this is the ordinary PATH lookup; on Windows it is the whole point of
    resolve_binary, since PATHEXT is what turns the bare name into a real file.
    """
    if os.name == "nt":
        shim = tmp_path / "councilprobe.cmd"
        shim.write_text("@echo off\r\necho SHIM OK\r\n", encoding="utf-8")
    else:
        shim = tmp_path / "councilprobe"
        shim.write_text("#!/bin/sh\necho 'SHIM OK'\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    resolved = resolve_binary("councilprobe")
    assert Path(resolved).is_file()

    reply = asyncio.run(
        run_process(["councilprobe"], cwd=str(tmp_path), timeout=30)
    )
    assert reply.ok is True
    assert "SHIM OK" in reply.text


def test_a_failed_run_still_hands_back_what_it_printed(tmp_path):
    """A non-zero exit must not throw away stdout.

    Harnesses report their own failures in their own output format — `claude -p`
    exits 1 for an expired login and still emits a `result` event naming the cause.
    Without the raw output the adapter has nothing to read, and the user is shown a
    two-kilobyte tail of JSONL instead of one sentence.
    """
    script = tmp_path / "boom.py"
    script.write_text(
        'print(\'{"type":"result","is_error":true,"result":"auth failed"}\')\n'
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    reply = asyncio.run(
        run_process([sys.executable, str(script)], cwd=str(tmp_path), timeout=30)
    )
    assert reply.ok is False
    assert reply.exit_code == 1
    assert "auth failed" in reply.text


def test_a_glob_binary_resolves_to_the_newest_match(tmp_path):
    """Some harnesses live under a directory whose name changes on every update.

    The ChatGPT desktop app installs codex as `.../Codex/bin/<content-hash>/codex.exe`
    and mints a new hash each time it updates itself — one rotated mid-session while
    this was being written. That directory is on no PATH, so it has to be named, and
    naming it literally produces a config line with an expiry date.
    """
    old = tmp_path / "aaa11"
    new = tmp_path / "bbb22"
    for directory in (old, new):
        directory.mkdir()
        (directory / "harness.exe").write_text("x", encoding="utf-8")
    os.utime(old / "harness.exe", (1_000_000, 1_000_000))
    os.utime(new / "harness.exe", (2_000_000, 2_000_000))

    resolved = resolve_binary(str(tmp_path / "*" / "harness.exe"))
    assert Path(resolved) == new / "harness.exe"


def test_a_glob_that_matches_nothing_is_handed_back_for_the_error_message(tmp_path):
    pattern = str(tmp_path / "*" / "absent.exe")
    assert resolve_binary(pattern) == pattern


def test_a_glob_binary_actually_runs(tmp_path):
    """End to end: the pattern reaches CreateProcess as a real, startable file.

    Only argv[0] is resolved — a glob anywhere else is an argument, and rewriting a
    panelist's arguments is not this function's business.
    """
    versioned = tmp_path / "hash1"
    versioned.mkdir()
    if os.name == "nt":
        shim = versioned / "probe.cmd"
        shim.write_text("@echo off\r\necho GLOB OK\r\n", encoding="utf-8")
    else:
        shim = versioned / "probe"
        shim.write_text("#!/bin/sh\necho 'GLOB OK'\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    reply = asyncio.run(
        run_process([str(tmp_path / "*" / shim.name)], cwd=str(tmp_path), timeout=30)
    )
    assert reply.ok is True and "GLOB OK" in reply.text
