"""The daemon putting itself away — and, much more importantly, refusing to.

A control plane that shuts down while a panel is mid-argument throws away minutes of
real model calls and the user's own quota. Every test here that matters is a test that
it stays up.
"""

import asyncio

import pytest

from council import shortcut
from council.server.idle import Idle, watch


@pytest.fixture()
def frozen(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr("council.server.idle.time.monotonic", lambda: now["t"])
    return now


def test_an_open_tab_keeps_it_up(frozen):
    idle = Idle(seconds=10.0, busy=lambda: False)
    idle.opened()
    frozen["t"] = 1000.0
    assert idle.spent() is False


def test_it_goes_once_the_last_tab_closes(frozen):
    idle = Idle(seconds=10.0, busy=lambda: False)
    idle.opened()
    idle.closed()
    assert idle.spent() is False, "not immediately — a reload is not a departure"
    frozen["t"] = 11.0
    assert idle.spent() is True


def test_it_will_not_go_while_a_council_is_running(frozen):
    """The one that matters. Nobody watching is not a reason to throw away the work."""
    running = {"yes": True}
    idle = Idle(seconds=10.0, busy=lambda: running["yes"])
    idle.opened()
    idle.closed()
    frozen["t"] = 10_000.0
    assert idle.spent() is False, "a running council outranks an empty browser"

    running["yes"] = False
    assert idle.spent() is False, "and then it gets the full grace period, not none of it"
    frozen["t"] = 10_011.0
    assert idle.spent() is True


def test_a_stream_that_never_opened_cannot_make_it_immortal(frozen):
    """A failed stream still runs its cleanup; the count must not go negative."""
    idle = Idle(seconds=10.0, busy=lambda: False)
    idle.closed()
    idle.closed()
    assert idle.streams == 0
    frozen["t"] = 11.0
    assert idle.spent() is True


def test_one_tab_of_several_closing_is_not_the_last_one(frozen):
    idle = Idle(seconds=10.0, busy=lambda: False)
    idle.opened()
    idle.opened()
    idle.closed()
    frozen["t"] = 100.0
    assert idle.spent() is False


def test_the_watchdog_asks_the_server_to_stop_once():
    # Real time, and a window small enough to pass in one tick. Not the `frozen` clock:
    # with time held still the loop would be correct and never finish.
    stopped = []
    idle = Idle(seconds=0.0001, busy=lambda: False)
    idle.stop = lambda: stopped.append(True)
    asyncio.run(watch(idle, tick=0.001))
    assert stopped == [True], "asks once, then stops asking"


def test_the_shortcut_asks_for_a_daemon_that_lets_go():
    argv = shortcut.arguments(90.0)
    assert argv[:3] == ["-m", "council", "up"]
    assert argv[argv.index("--exit-when-idle") + 1] == "90.0"
    assert "--port" not in argv, "no port unless one was asked for"
    assert shortcut.arguments(90.0, port=9000)[-2:] == ["--port", "9000"]


def test_the_shortcut_avoids_a_console_flash():
    """`python.exe` would flash a window on every launch; `pythonw.exe` does not."""
    chosen = shortcut.interpreter()
    assert chosen.endswith(("pythonw.exe", "pythonw", "python.exe", "python", "python3"))


def test_a_desktop_entry_is_written_where_it_was_asked_for(tmp_path):
    path = shortcut._freedesktop(
        tmp_path / "council.desktop",
        [shortcut.interpreter(), *shortcut.arguments(90.0)],
    )
    body = path.read_text(encoding="utf-8")
    assert path.parent == tmp_path
    assert "[Desktop Entry]" in body
    assert "--exit-when-idle" in body and "--app" in body


def test_the_close_button_works_on_a_daemon_that_never_idles():
    """`--exit-when-idle` is opt-in; being able to close it from the UI is not.

    `stop` and the idle policy live on the same object, so an `Idle(0)` that could not
    be stopped would mean the Close button quietly doing nothing on every daemon
    started by hand — which is most of them.
    """
    idle = Idle(seconds=0.0, busy=lambda: False)
    stopped = []
    idle.stop = lambda: stopped.append(True)
    assert idle.spent() is False, "seconds=0 means never idle out"
    idle.stop()
    assert stopped == [True], "and yet it can still be told to stop"


def test_the_shortcut_opens_a_window_rather_than_a_tab():
    """Closing one tab of twenty is ambiguous; closing Council's own window is not."""
    argv = shortcut.arguments(90.0)
    assert "--app" in argv
    assert "--open" not in argv, "--app supersedes it, and passing both is a contradiction"
