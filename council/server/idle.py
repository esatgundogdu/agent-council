"""Letting the daemon put itself away when nobody is looking.

A control plane you have to remember to shut down is one you leave running, and a
long-lived local server nobody asked for is exactly the kind of thing that quietly
collects ports and confusion. So the desktop shortcut starts a daemon that ends with
the window that opened it: close the tab, and a little later the server is gone.

**What counts as looking.** Every open tab holds an SSE connection — the session list
subscribes to `/api/events` and a session view subscribes to its own stream — so the
count of live streams *is* the count of open tabs, without asking the browser anything.
`council wait` holds one too, which is the answer for an agent waiting on a council: as
long as something is genuinely attached, the daemon stays.

**What it will never do** is exit while a council is running. A panel mid-argument is
minutes of real model calls and the user's own quota, and no amount of "nobody is
watching" makes throwing that away correct. The timer only starts once the last stream
has closed *and* the last session has finished, whichever comes later.

Off unless asked for. A daemon started by hand, or brought up by a CLI command that
needed one, keeps the behaviour it always had: it stays until it is stopped.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable


class Idle:
    """The count of who is attached, and the clock that runs when nobody is."""

    def __init__(self, seconds: float, busy: Callable[[], bool]) -> None:
        #: How long everything has to stay quiet before the daemon lets go.
        self.seconds = seconds
        #: Whether any council is still running. Asked every tick, never cached.
        self.busy = busy
        #: Live event streams. One per open tab, plus one per waiting client.
        self.streams = 0
        self.since = time.monotonic()
        #: Set by whoever owns the server; calling it asks for a graceful shutdown.
        self.stop: Callable[[], None] | None = None

    def opened(self) -> None:
        self.streams += 1

    def closed(self) -> None:
        # Never below zero: a stream that fails to open and still gets cleaned up
        # would otherwise leave the count negative and the daemon immortal.
        self.streams = max(0, self.streams - 1)
        self.since = time.monotonic()

    def spent(self) -> bool:
        """Has the daemon been left alone for long enough to go?"""
        if self.streams or self.busy():
            # Not idle, so the clock has not started. Resetting it here rather than on
            # each event is what makes the grace period a period of *quiet* — a council
            # that ran for an hour unwatched still gets its full grace after it lands.
            self.since = time.monotonic()
            return False
        return time.monotonic() - self.since >= self.seconds


async def watch(idle: Idle, tick: float = 1.0) -> None:
    """Ask once a second, and ask the server to stop the first time the answer is yes."""
    while True:
        await asyncio.sleep(tick)
        if idle.spent():
            if idle.stop is not None:
                idle.stop()
            return
