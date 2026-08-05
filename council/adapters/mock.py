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

from .base import Adapter, Delta, Reply

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

#: The line every prompt that wants an envelope carries, and no other prompt does.
#: Which reply to script used to be decided by counting calls — "the first one is the
#: plan" — which is only true of the modes that open with Phase 1. A consultation has
#: no Phase 1, so its single call was answered with a plan and every panelist came back
#: malformed. Reading the contract off the prompt is both simpler and always right.
ENVELOPE_ASKED_FOR = '"verdict": "CONTINUE or READY"'


class MockAdapter(Adapter):
    name = "mock"

    def __init__(self, model: str | None = None, **kwargs):
        super().__init__(model=model, **kwargs)
        self.panelist_name = kwargs.get("panelist_name", "default")
        self.scenario = _load_scenario(kwargs.get("scenario_path"))
        self._calls = 0
        self._turns = 0
        #: Session id passed in on each call, for tests to assert continuity.
        self.sessions_seen: list[str | None] = []

    @property
    def _script(self) -> dict:
        return self.scenario.get(self.panelist_name) or self.scenario.get("default", {})

    async def ask(
        self,
        prompt: str,
        cwd: str,
        timeout: int,
        session: str | None = None,
        on_delta=None,
    ) -> Reply:
        script = self._script
        self.sessions_seen.append(session)
        delay = float(script.get("delay", 0))
        if delay:
            await asyncio.sleep(min(delay, timeout))

        first_call = self._calls == 0
        self._calls += 1
        session_id = session or f"mock-session-{self.panelist_name}"
        if script.get("no_session"):
            session_id = None  # harness that cannot resume: exercises the fallback

        if ENVELOPE_ASKED_FOR in prompt:
            turn_index = self._turns
            self._turns += 1
            if script.get("fail_at_turn") == turn_index:
                return Reply(ok=False, error="mock: scripted turn failure")
            turns = script.get("turns") or DEFAULT_TURNS
            text = _personalise(
                turns[min(turn_index, len(turns) - 1)], self.panelist_name
            )
        else:
            if first_call and script.get("fail_phase1"):
                return Reply(ok=False, error="mock: scripted phase-1 failure")
            text = _personalise(script.get("plan", DEFAULT_PLAN), self.panelist_name)

        # Whatever this call is, if it is the first one the panelist is meeting the
        # repository — so that is when it reads files.
        await self._stream(text, session_id, first_call, on_delta, script)
        return Reply(ok=True, text=text, session_id=session_id)

    async def _stream(self, text, session_id, is_phase1, on_delta, script) -> None:
        """Replay the reply as deltas, so the live path is exercised without a model."""
        if on_delta is None:
            return
        if session_id:
            on_delta(Delta(kind="session", session_id=session_id))
        pace = float(script.get("stream_delay", 0))
        if is_phase1:
            for target in script.get("reads") or ("README.md", "council/panel.py"):
                on_delta(Delta(kind="tool", tool="read", target=target))
                if pace:
                    await asyncio.sleep(pace)
        for i in range(0, len(text), 80):
            on_delta(Delta(kind="text", text=text[i : i + 80]))
            if pace:
                await asyncio.sleep(pace)
        on_delta(Delta(kind="usage", tokens=max(1, len(text) // 4)))


def _personalise(text: str, name: str) -> str:
    return text.replace("{panelist}", name)


def _load_scenario(path: str | Path | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("mock scenario must be a JSON object keyed by panelist name")
    return data
