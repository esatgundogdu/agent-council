"""Phase management: independent plans, the debate, the digest."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import PROMPTS_DIR
from .adapters import Adapter, AdapterError, Reply, build_adapter
from .config import CouncilConfig
from .envelope import CONTINUE, READY, parse_envelope
from .panel import Panelist, identity_map
from .transcript import Transcript, Turn, estimate_tokens

MIN_PANEL = 2


class CouncilError(Exception):
    """The session cannot continue (e.g. too few panelists left)."""


@dataclass
class SessionPaths:
    root: Path

    @property
    def task(self) -> Path:
        return self.root / "task.md"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.md"

    @property
    def digest(self) -> Path:
        return self.root / "digest.md"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    def prepare(self) -> None:
        self.plans_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Result:
    rounds: int
    termination: str
    panel: list[Panelist]
    dropped: list[tuple[str, str]] = field(default_factory=list)
    tokens: int = 0
    duration: float = 0.0


class Council:
    def __init__(
        self,
        config: CouncilConfig,
        panel: list[Panelist],
        paths: SessionPaths,
        project_dir: Path,
        adapters: dict[str, Adapter] | None = None,
        prompts_dir: Path | None = None,
        opencode_config: Path | None = None,
        scenario_path: str | Path | None = None,
        progress=None,
    ):
        self.config = config
        self.panel = list(panel)
        self.paths = paths
        self.project_dir = Path(project_dir)
        self.prompts_dir = Path(prompts_dir or PROMPTS_DIR)
        self.opencode_config = opencode_config
        self.scenario_path = scenario_path
        self.progress = progress or (lambda msg: None)

        self.transcript = Transcript()
        self.task_text = ""
        self.tokens_used = 0
        self.dropped: list[tuple[str, str]] = []
        self.started = time.monotonic()
        self._adapters = adapters or self._build_adapters()
        self._templates = {
            name: (self.prompts_dir / f"{name}.md").read_text(encoding="utf-8")
            for name in (
                "independent_plan",
                "discussion_turn",
                "discussion_turn_session",
                "compaction",
            )
        }
        # Session continuity: each panelist keeps talking in the harness conversation
        # where it explored the repo, so it does not lose that context between rounds.
        self.sessions: dict[str, str] = {}
        self._seen: dict[str, int] = {}  # transcript turns already shown to each agent
        self._plans_shown: set[str] = set()

    # ---- infrastructure --------------------------------------------------

    def _build_adapters(self) -> dict[str, Adapter]:
        adapters: dict[str, Adapter] = {}
        for p in self.panel:
            kwargs: dict = {"model": p.model, "variant": p.variant}
            if p.adapter == "opencode_cli" and self.opencode_config:
                kwargs["config_path"] = self.opencode_config
            if p.adapter == "mock":
                kwargs["panelist_name"] = p.name
                kwargs["scenario_path"] = self.scenario_path
            adapters[p.label] = build_adapter(p.adapter, **kwargs)
        return adapters

    def _event(self, kind: str, **payload) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": kind,
            **payload,
        }
        with self.paths.events.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _status(self, state: str, **payload) -> None:
        # `pid` lets a reader tell a live run from a corpse: a status still saying
        # "running" whose pid is dead (os.kill(pid, 0) raises) was interrupted, not
        # progressing. `updated_at` is the heartbeat that backs that up.
        record = {
            "state": state,
            "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(time.monotonic() - self.started, 1),
            "tokens": self.tokens_used,
            **payload,
        }
        self.paths.status.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _account(self, prompt: str, reply: Reply) -> int:
        """Add this call's tokens to the running total, and return the delta.

        opencode reports real usage; codex/claude do not, so those fall back to a
        char/4 estimate. The delta is stashed on the reply so callers can log the
        per-turn cost without recomputing it.
        """
        delta = reply.tokens
        if delta is None:
            delta = estimate_tokens(prompt) + estimate_tokens(reply.text)
        reply.meta["effective_tokens"] = delta
        self.tokens_used += delta
        return delta

    async def _ask(
        self,
        panelist: Panelist,
        prompt: str,
        timeout: int,
        use_session: bool = True,
    ) -> Reply:
        """Send a prompt to a panelist, continuing its own session where possible.

        `use_session=False` forces a cold call — used for compaction, whose summarising
        request must not land in the middle of a panelist's debate context, and for the
        fallback retry when a resumed session turns out to be unusable.
        """
        adapter = self._adapters[panelist.label]
        session = None
        if use_session and self.config.protocol.session_continuity:
            session = self.sessions.get(panelist.label)
        try:
            reply = await adapter.ask(
                prompt, cwd=str(self.project_dir), timeout=timeout, session=session
            )
        except AdapterError as exc:
            reply = Reply(ok=False, error=str(exc))
        self._account(prompt, reply)
        if use_session and reply.session_id:
            self.sessions[panelist.label] = reply.session_id
        return reply

    def _has_session(self, panelist: Panelist) -> bool:
        return bool(
            self.config.protocol.session_continuity
            and self.sessions.get(panelist.label)
        )

    def _mark_caught_up(self, panelist: Panelist) -> None:
        """Record what this panelist has now been shown, so later turns send only deltas."""
        self._seen[panelist.label] = len(self.transcript.turns)
        self._plans_shown.add(panelist.label)

    def _drop(self, panelist: Panelist, why: str) -> None:
        self.panel = [p for p in self.panel if p.label != panelist.label]
        self.dropped.append((panelist.label, why))
        self._event("panelist_dropped", agent=panelist.label, reason=why)
        self.progress(f"  ! {panelist.label} dropped from the panel: {why}")

    def _require_quorum(self) -> None:
        if len(self.panel) < MIN_PANEL:
            names = ", ".join(f"{lbl} ({why})" for lbl, why in self.dropped)
            raise CouncilError(
                f"only {len(self.panel)} panelist(s) left, need at least {MIN_PANEL}. "
                f"Dropped: {names or 'none'}"
            )

    # ---- phases ----------------------------------------------------------

    async def run(self) -> Result:
        self.paths.prepare()
        self.task_text = self.paths.task.read_text(encoding="utf-8")
        self._event(
            "session_start",
            project_dir=str(self.project_dir),
            identities=identity_map(self.panel),
            panel=[{"label": p.label, "name": p.name, "model": p.model} for p in self.panel],
        )
        try:
            await self.phase1()
            rounds, termination = await self.phase2()
            self.phase3(rounds, termination)
        except CouncilError as exc:
            self._event("session_failed", error=str(exc))
            self._status("failed", error=str(exc))
            raise

        result = Result(
            rounds=rounds,
            termination=termination,
            panel=self.panel,
            dropped=self.dropped,
            tokens=self.tokens_used,
            duration=time.monotonic() - self.started,
        )
        self._event(
            "session_end",
            rounds=rounds,
            termination=termination,
            tokens=self.tokens_used,
        )
        self._status(
            "done", phase=3, rounds=rounds, termination=termination,
            digest=str(self.paths.digest),
        )
        return result

    async def phase1(self) -> None:
        self.progress(
            f"Phase 1: {len(self.panel)} panelists exploring the repository in parallel..."
        )
        self._status("running", phase=1, round=0)
        prompt = self._templates["independent_plan"].replace("{task}", self.task_text)
        timeout = self.config.timeouts.per_call_phase1

        self._event("phase1_start", prompt_chars=len(prompt))

        # Persist each plan the moment its call returns, not after the whole batch:
        # a crash after three of four panelists reply must keep those three plans.
        panel = list(self.panel)
        tasks = {p.label: asyncio.create_task(self._plan_one(p, prompt, timeout)) for p in panel}
        replies: dict[str, Reply] = {}
        for coro in asyncio.as_completed(list(tasks.values())):
            label, reply = await coro
            replies[label] = reply

        # Drops are applied in panel order, independent of completion order, so the
        # digest's and the quorum error's "dropped" list stay deterministic.
        for panelist in panel:
            reply = replies[panelist.label]
            if not (reply.ok and reply.text.strip()):
                self._drop(panelist, reply.error or "empty plan")

        self._require_quorum()

    async def _plan_one(self, panelist: Panelist, prompt: str, timeout: int):
        """Ask one panelist for its plan and commit it to disk before returning.

        Each call is isolated — the Phase-1 prompt carries only the task, never
        another panelist's plan — so running these concurrently keeps them independent.
        """
        reply = await self._ask(panelist, prompt, timeout)
        if reply.ok and reply.text.strip():
            path = self.paths.plans_dir / f"agent-{panelist.letter.lower()}.md"
            path.write_text(
                f"# {panelist.label} — independent plan\n\n{reply.text.strip()}\n",
                encoding="utf-8",
            )
            self.transcript.plans[panelist.label] = reply.text.strip()
            self._event(
                "plan_received",
                agent=panelist.label,
                chars=len(reply.text),
                seconds=round(reply.duration, 1),
                tokens=reply.meta.get("effective_tokens"),
                # Recorded so a future resume can reconnect to the same conversation.
                session=reply.session_id,
            )
            self.progress(
                f"  + {panelist.label} plan ready ({len(reply.text)} chars, "
                f"{reply.duration:.0f}s)"
            )
        return panelist.label, reply

    async def phase2(self) -> tuple[int, str]:
        protocol = self.config.protocol
        self.progress(
            f"Phase 2: discussion (min {protocol.min_rounds}, max {protocol.max_rounds} rounds)"
        )
        round_no = 0
        termination = "max_rounds"

        while round_no < protocol.max_rounds:
            round_no += 1
            self.progress(f"  Round {round_no}:")
            self._status("running", phase=2, round=round_no)
            self._event("round_start", round=round_no, panel=[p.label for p in self.panel])

            expected = [p.label for p in self.panel]
            for panelist in list(self.panel):
                if panelist.label not in {p.label for p in self.panel}:
                    continue  # dropped earlier in this same round
                await self._take_turn(panelist, round_no)
                self._require_quorum()

                budget = self._budget_exceeded()
                if budget:
                    self._event("terminated", reason=budget, round=round_no)
                    return round_no, budget

            expected = [lbl for lbl in expected if lbl in {p.label for p in self.panel}]
            if self.transcript.all_ready(round_no, expected):
                if round_no >= protocol.min_rounds:
                    self._event("terminated", reason="all_ready", round=round_no)
                    self.progress(f"  All panelists READY after round {round_no}.")
                    return round_no, "all_ready"
                self.progress(
                    f"  All READY, but min_rounds={protocol.min_rounds} — continuing."
                )
                self._event("early_ready_ignored", round=round_no)

            if round_no < protocol.max_rounds:
                await self._maybe_compact()

        self._event("terminated", reason="max_rounds", round=round_no)
        self.progress(f"  Reached max_rounds ({protocol.max_rounds}).")
        return round_no, termination

    async def _take_turn(self, panelist: Panelist, round_no: int) -> None:
        resumed = self._has_session(panelist)
        if resumed:
            prompt = self._session_prompt(panelist, round_no)
        else:
            prompt = self._discussion_prompt(panelist, round_no)
        reply = await self._ask(panelist, prompt, self.config.timeouts.per_call)

        if not reply.ok and resumed:
            # The session was refused, expired or lost. Fall back to the stateless
            # path — a full reassembly always reconstructs the conversation.
            self._event(
                "session_fallback",
                agent=panelist.label,
                round=round_no,
                error=reply.error,
            )
            self.progress(f"    ~ {panelist.label} session unusable, retrying cold")
            # Dropping the id is what makes this call cold. Leaving `use_session` on
            # means the fresh conversation it opens is adopted, so only this seam turn
            # pays for a full reassembly and the rest of the run runs warm again.
            self.sessions.pop(panelist.label, None)
            prompt = self._discussion_prompt(panelist, round_no)
            reply = await self._ask(panelist, prompt, self.config.timeouts.per_call)

        if not reply.ok:
            note = reply.error or "no response"
            self.transcript.add(Turn.failure(round_no, panelist.label, note))
            self._event("turn_failed", agent=panelist.label, round=round_no, error=note)
            self.progress(f"    ! {panelist.label} failed: {note[:120]}")
            if self.config.on_failure == "abort":
                raise CouncilError(f"{panelist.label} failed: {note}")
            self._drop(panelist, note)
            return

        envelope = parse_envelope(reply.text)
        self.transcript.add(Turn.from_envelope(round_no, panelist.label, envelope))
        self._mark_caught_up(panelist)
        # The full comment/reason go in the event log, not just their length: the
        # in-memory transcript is the only other copy, and a crash before Phase 3
        # (which writes transcript.md) would otherwise lose every argument made.
        self._event(
            "turn",
            agent=panelist.label,
            round=round_no,
            verdict=envelope.verdict,
            malformed=envelope.malformed,
            chars=len(reply.text),
            seconds=round(reply.duration, 1),
            tokens=reply.meta.get("effective_tokens"),
            comment=envelope.comment,
            reason=envelope.reason,
            session=reply.session_id,
            resumed=resumed,
        )
        flag = " (malformed envelope)" if envelope.malformed else ""
        self.progress(
            f"    {panelist.label}: {envelope.verdict}{flag} "
            f"({reply.duration:.0f}s, {self.tokens_used} tok used)"
        )

    def _session_prompt(self, panelist: Panelist, round_no: int) -> str:
        """Only what this panelist has not seen — its own context lives in its session."""
        blocks: list[str] = []

        if panelist.label not in self._plans_shown:
            others = {
                label: text
                for label, text in self.transcript.plans.items()
                if label != panelist.label
            }
            if others:
                rendered = "\n\n---\n\n".join(
                    f"## {label}'s plan\n\n{others[label].strip()}"
                    for label in sorted(others)
                )
                blocks.append(
                    "# THE OTHER PANELISTS' PLANS\n\nThe others wrote these "
                    "independently, without seeing yours.\n\n" + rendered
                )

        new_turns = self.transcript.turns[self._seen.get(panelist.label, 0) :]
        if new_turns:
            blocks.append(
                "# SAID SINCE YOU LAST SPOKE\n\n"
                + "\n\n".join(t.render() for t in new_turns)
            )
        elif not blocks:
            blocks.append("# NOTHING NEW\n\nNo one has spoken since your last turn.")

        return (
            self._templates["discussion_turn_session"]
            .replace("{agent_label}", panelist.label)
            .replace("{new_material}", "\n\n".join(blocks))
            .replace("{round_no}", str(round_no))
        )

    def _discussion_prompt(self, panelist: Panelist, round_no: int) -> str:
        return (
            self._templates["discussion_turn"]
            .replace("{agent_label}", panelist.label)
            .replace("{task}", self.task_text)
            .replace("{plans}", self.transcript.render_plans(panelist))
            .replace("{conversation}", self.transcript.render_conversation())
            .replace("{round_no}", str(round_no))
        )

    def _budget_exceeded(self) -> str | None:
        protocol = self.config.protocol
        if self.tokens_used >= protocol.token_budget:
            self.progress(
                f"  Token budget reached ({self.tokens_used}/{protocol.token_budget})."
            )
            return "token_budget"
        elapsed = time.monotonic() - self.started
        if elapsed >= protocol.wall_clock_budget:
            self.progress(f"  Time budget reached ({elapsed:.0f}s).")
            return "wall_clock_budget"
        return None

    async def _maybe_compact(self) -> None:
        threshold = self.config.protocol.compaction_threshold
        if not self.transcript.needs_compaction(threshold):
            return
        rounds = self.transcript.rounds_to_compact()
        if not rounds:
            return

        panelist = self._compaction_panelist()
        if panelist is None:
            return
        prompt = self._templates["compaction"].replace(
            "{conversation}", self.transcript.text_to_compact(rounds)
        )
        self.progress(f"    (compacting rounds {rounds[0]}-{rounds[-1]}...)")
        # Cold call on purpose: a summarising request must not be injected into the
        # panelist's own debate session, where it would pollute its context.
        reply = await self._ask(
            panelist, prompt, self.config.timeouts.per_call, use_session=False
        )
        if reply.ok and reply.text.strip():
            self.transcript.apply_compaction(reply.text, rounds[-1])
            self._event("compacted", through_round=rounds[-1], agent=panelist.label)
        else:
            # Compaction is an optimisation; losing it costs tokens, not correctness.
            self._event("compaction_failed", error=reply.error)

    def _compaction_panelist(self) -> Panelist | None:
        """Who summarises the transcript.

        Falls back to the strongest harnesses first: everything the panel remembers
        about earlier rounds is whatever this model chose to keep.
        """
        wanted = self.config.protocol.compaction_panelist
        if wanted:
            for p in self.panel:
                if p.name == wanted:
                    return p
        for adapter in ("codex_cli", "claude_cli", "opencode_cli"):
            for p in self.panel:
                if p.adapter == adapter:
                    return p
        return self.panel[0] if self.panel else None

    def phase3(self, rounds: int, termination: str) -> None:
        self.progress("Phase 3: writing transcript and digest...")
        header = (
            f"Panel of {len(self.panel)} · {rounds} round(s) · "
            f"ended: {TERMINATION_LABELS.get(termination, termination)}"
        )
        self.paths.transcript.write_text(
            self.transcript.render_transcript_md(header), encoding="utf-8"
        )
        self.paths.digest.write_text(
            self.render_digest(rounds, termination), encoding="utf-8"
        )

    def render_digest(self, rounds: int, termination: str) -> str:
        lines = [
            "# Council digest",
            "",
            f"- **Panelists:** {len(self.panel)} ({', '.join(p.label for p in self.panel)})",
            f"- **Rounds held:** {rounds}",
            f"- **Ended because:** {TERMINATION_LABELS.get(termination, termination)}",
        ]
        if self.dropped:
            dropped = "; ".join(f"{lbl} ({why[:80]})" for lbl, why in self.dropped)
            lines.append(f"- **Dropped mid-session:** {dropped}")
        if termination != "all_ready":
            lines.append(
                "- **Note:** the discussion was cut short — positions below may not be final."
            )
        lines += ["", "## Final position of each panelist", ""]

        unresolved: list[tuple[str, str]] = []
        for panelist in self.panel:
            turn = self.transcript.last_turn_of(panelist.label)
            lines.append(f"### {panelist.label} — {turn.verdict if turn else 'no reply'}")
            lines.append("")
            if turn is None:
                lines += ["_never completed a turn_", ""]
                continue
            lines += [turn.comment.strip() or "_(empty)_", ""]
            if turn.reason:
                label = "Why ready" if turn.verdict == READY else "Still open"
                lines += [f"**{label}:** {turn.reason.strip()}", ""]
            if turn.verdict == CONTINUE:
                unresolved.append(
                    (panelist.label, turn.reason.strip() or "see final position above")
                )

        lines += ["## Points still open when the session ended", ""]
        if not unresolved:
            lines.append(
                "Every panelist ended on READY; no panelist recorded an outstanding objection."
            )
        else:
            lines.append(
                "These panelists had not accepted the plan as settled. Each item below is a "
                "decision for the user:"
            )
            lines.append("")
            for label, why in unresolved:
                lines.append(f"- **{label}:** {why}")
        lines += [
            "",
            "---",
            "",
            f"Full discussion: `{self.paths.transcript.name}` · "
            f"independent plans: `plans/` · "
            f"token estimate: {self.tokens_used}",
            "",
        ]
        return "\n".join(lines)


TERMINATION_LABELS = {
    "all_ready": "every panelist signalled READY",
    "max_rounds": "the round limit was reached",
    "token_budget": "the token budget was reached",
    "wall_clock_budget": "the time budget was reached",
}
