"""Daemon-level change notification.

Session events have their own per-session log; this is the coarser signal — "the set
of sessions changed" — that keeps a browser's sidebar honest when a run is started
from the CLI, or finishes while nobody is looking at it.
"""

from __future__ import annotations

import asyncio


class Waiter:
    def __init__(self, hub: "Hub") -> None:
        self._hub = hub
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> bool:
        """True if something changed, False if `timeout` elapsed first."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self._event.clear()
        return True

    def close(self) -> None:
        self._hub.unsubscribe(self)

    def __enter__(self) -> "Waiter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Hub:
    def __init__(self) -> None:
        self._waiters: set[Waiter] = set()

    def subscribe(self) -> Waiter:
        waiter = Waiter(self)
        self._waiters.add(waiter)
        return waiter

    def unsubscribe(self, waiter: Waiter) -> None:
        self._waiters.discard(waiter)

    def publish(self) -> None:
        for waiter in list(self._waiters):
            waiter.notify()
