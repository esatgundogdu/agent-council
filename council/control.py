"""Out-of-band control of a running session.

Every command lands here and is applied at a **turn boundary**. A harness call is
atomic — it can be killed, never steered mid-flight — so pausing, dropping a panelist
or injecting a message all wait for the current turn to finish. That is what keeps the
transcript coherent no matter what the user does to a run while it is in progress.

Commands arrive from the UI and from the main agent through the same API, and each one
is recorded as a `control` event, so the log says who intervened, when, and how.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

#: Fields of `protocol` a running session will accept new values for.
EXTENDABLE = {"max_rounds", "token_budget", "wall_clock_budget", "min_rounds"}

ACTIONS = (
    "pause", "resume", "stop", "skip", "drop", "restore", "extend", "digest", "chair",
)


class ControlError(ValueError):
    """A control command that cannot be carried out as asked."""


@dataclass
class Controller:
    """The mutable intentions of whoever is watching the run."""

    paused: bool = False
    #: "graceful" (write the digest from what exists) or "hard" (abandon the session).
    stop: str | None = None
    digest_now: bool = False
    skip_once: set[str] = field(default_factory=set)
    drop: set[str] = field(default_factory=set)
    restore: set[str] = field(default_factory=set)
    chair: list[tuple[str, str]] = field(default_factory=list)
    extend: dict[str, int] = field(default_factory=dict)
    #: The panel's anonymous labels, so a command can be checked against
    #: reality rather than accepted and silently discarded.
    labels: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._gate = asyncio.Event()
        self._gate.set()
        #: Set by the runtime so each command is logged where it happened.
        self.on_command = None

    # ---- commands --------------------------------------------------------

    def command(self, action: str, by: str = "user", **payload) -> dict:
        if action not in ACTIONS:
            raise ControlError(
                f"unknown action '{action}'. Known: {', '.join(ACTIONS)}"
            )
        detail = getattr(self, f"_{action}")(**payload)
        record = {"action": action, "by": by, "detail": detail}
        if self.on_command is not None:
            self.on_command(record)
        return record

    def _pause(self) -> str:
        self.paused = True
        self._gate.clear()
        return "will hold after the current turn"

    def _resume(self) -> str:
        self.paused = False
        self._gate.set()
        return "resumed"

    def _stop(self, how: str = "graceful") -> str:
        if how not in ("graceful", "hard"):
            raise ControlError("stop takes how='graceful' or how='hard'")
        self.stop = how
        self._gate.set()  # a paused run must still be stoppable
        return how

    def _digest(self) -> str:
        self.digest_now = True
        self._gate.set()
        return "digest after the current round"

    def _skip(self, agent: str) -> str:
        self.skip_once.add(self._label(agent))
        return self._label(agent)

    def _drop(self, agent: str) -> str:
        label = self._label(agent)
        self.drop.add(label)
        self.restore.discard(label)
        return label

    def _restore(self, agent: str) -> str:
        label = self._label(agent)
        self.restore.add(label)
        self.drop.discard(label)
        return label

    def _label(self, agent: object) -> str:
        """The panelist this command is about, or an error naming the real ones.

        `drop Agent-Z`, `drop gamma` (the name in council.yaml) and `skip 7` all used
        to return 200 and then quietly do nothing: the orchestrator matches on the
        anonymous label, and anonymisation shuffles which name that is, so the one
        spelling that works is the one the user is least able to guess.
        """
        if not isinstance(agent, str) or not agent.strip():
            raise ControlError(
                "this action needs a panelist, e.g. agent='Agent-A'"
                + (f". On this panel: {', '.join(sorted(self.labels))}" if self.labels else "")
            )
        wanted = agent.strip()
        if not self.labels:  # the panel is not known yet; the orchestrator will match
            return wanted
        for label in self.labels:
            if label.lower() == wanted.lower():
                return label
        raise ControlError(
            f"no panelist called {wanted!r}. On this panel: "
            f"{', '.join(sorted(self.labels))}"
        )

    def _chair(self, text: str, author: str = "user") -> str:
        text = (text or "").strip()
        if not text:
            raise ControlError("a chair message cannot be empty")
        self.chair.append((text, author))
        return text[:120]

    def _extend(self, **values) -> str:
        unknown = set(values) - EXTENDABLE
        if unknown:
            raise ControlError(
                f"cannot change {', '.join(sorted(unknown))} mid-run. "
                f"Extendable: {', '.join(sorted(EXTENDABLE))}"
            )
        if not values:
            raise ControlError(
                f"extend needs a field to change: {', '.join(sorted(EXTENDABLE))}"
            )
        applied = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ControlError(f"{key} must be a positive integer")
            self.extend[key] = value
            applied[key] = value
        return ", ".join(f"{k}={v}" for k, v in applied.items())

    # ---- what the orchestrator asks ---------------------------------------

    async def gate(self) -> None:
        """Block here while paused. Called between turns, never inside one."""
        if not self._gate.is_set():
            await self._gate.wait()

    def take_chair(self) -> list[tuple[str, str]]:
        pending, self.chair = self.chair, []
        return pending

    def take_extensions(self) -> dict[str, int]:
        pending, self.extend = self.extend, {}
        return pending

    def take_drops(self) -> set[str]:
        pending, self.drop = self.drop, set()
        return pending

    def take_restores(self) -> set[str]:
        pending, self.restore = self.restore, set()
        return pending

    def should_skip(self, label: str) -> bool:
        return label in self.skip_once

    def clear_skip(self, label: str) -> None:
        self.skip_once.discard(label)
