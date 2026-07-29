"""Scripted adapter: runs the whole protocol without touching a real model.

Drives the test suite and `council run --mock`. Scenarios are JSON:

    {
      "default": {"plan": "...", "turns": ["...", "..."]},
      "kimi": {"plan": "...", "turns": ["...{\\"verdict\\":\\"READY\\"}"],
                "fail_phase1": false, "delay": 0.0}
    }

A turn string is returned verbatim, so scenarios can exercise malformed envelopes,
prose verdicts and empty replies. When a panelist runs out of scripted turns the
last one repeats.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .base import Adapter, Reply

DEFAULT_PLAN = """## Approach

Mock plan for the task.

## Steps

1. Do the thing.
2. Verify the thing.

## Files to touch

- `mock.py`

## Risks

- This is a mock.

## Test strategy

- Run the mock tests.
"""

DEFAULT_TURNS = [
    '{"comment": "I largely agree with the other plans, but step 2 needs a test.",'
    ' "verdict": "CONTINUE", "reason": ""}',
    '{"comment": "The concern is addressed. Final position: proceed as agreed.",'
    ' "verdict": "READY", "reason": "All open points resolved."}',
]


class MockAdapter(Adapter):
    name = "mock"
    supports_sessions = True

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.panelist_name = kwargs.get("panelist_name", "default")
        self.scenario = _load_scenario(kwargs.get("scenario_path"))
        self._calls = 0
        #: Session id passed in on each call, for tests to assert continuity.
        self.sessions_seen: list[str | None] = []

    @property
    def _script(self) -> dict:
        return self.scenario.get(self.panelist_name) or self.scenario.get("default", {})

    async def ask(
        self, prompt: str, cwd: str, timeout: int, session: str | None = None
    ) -> Reply:
        script = self._script
        self.sessions_seen.append(session)
        delay = float(script.get("delay", 0))
        if delay:
            await asyncio.sleep(min(delay, timeout))

        is_phase1 = self._calls == 0
        self._calls += 1
        session_id = session or f"mock-session-{self.panelist_name}"
        if script.get("no_session"):
            session_id = None  # harness that cannot resume: exercises the fallback

        if is_phase1:
            if script.get("fail_phase1"):
                return Reply(ok=False, error="mock: scripted phase-1 failure")
            plan = script.get("plan", DEFAULT_PLAN)
            return Reply(
                ok=True,
                text=_personalise(plan, self.panelist_name),
                session_id=session_id,
            )

        turn_index = self._calls - 2
        if script.get("fail_at_turn") == turn_index:
            return Reply(ok=False, error="mock: scripted turn failure")

        turns = script.get("turns") or DEFAULT_TURNS
        text = turns[min(turn_index, len(turns) - 1)]
        return Reply(
            ok=True,
            text=_personalise(text, self.panelist_name),
            session_id=session_id,
        )


def _personalise(text: str, name: str) -> str:
    return text.replace("{panelist}", name)


def _load_scenario(path: str | Path | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("mock scenario must be a JSON object keyed by panelist name")
    return data
