"""Phase management: independent plans, the debate, the digest.

Four ways in, one protocol:

* ``independent`` — nobody is anchored. Every panelist explores the repo and writes its
  own plan before anyone sees anyone else's. The original design, and the default.
* ``review`` — a proposal already exists (typically the main agent's). Phase 1 is
  skipped and the panel goes straight to critiquing it. Faster, and deliberately
  anchored on the proposal's framing: the right mode for *verifying* a plan, the wrong
  one for *generating* one.
* ``hybrid`` — both. The panel writes independent plans first and only then meets the
  proposal, so the anchoring cannot happen before the panel has its own view.
* ``consult`` — no Phase 1 either, but nothing to critique yet. The panel opens on a
  brief: every panelist gives its own reading of the situation in the same round, in
  parallel and without seeing the others, and from round 2 it is an ordinary debate.
  Phase 1's independence at one call's price. ``max_rounds: 1`` stops after that opening
  round, which is the cheap answer to "is there a reason not to do this?" — and the one
  case where no panelist ever hears another.

Both a proposal (``seed.md``) and a context brief (``context.md``) are optional inputs,
and they are different things: the proposal is *the artefact being judged*, the brief is
*the situation around it* — what the agent that convened this council has already done
and decided. Where the brief may be shown is the one rule this module enforces on the
caller's behalf; see ``_context_block``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import PROMPTS_DIR
from .adapters import Adapter, AdapterError, Delta, Reply, build_adapter
from .calls import CallLog, call_filename
from .config import CouncilConfig
from .control import Controller
from .envelope import CONTINUE, READY, Envelope, parse_envelope
from .events import SCHEMA_VERSION, EventLog
from .panel import Panelist, identity_map
from .transcript import Transcript, Turn, estimate_tokens

MIN_PANEL = 2

#: How many turns in a row a panelist may fail before it leaves the panel. One
#: transient timeout is not evidence that a model is broken; three in a row is.
CONSECUTIVE_FAILURES_BEFORE_DROP = 3

MODES = ("independent", "review", "hybrid", "consult")

#: Modes in which the panel writes its own plan before the discussion.
PLANNING_MODES = ("independent", "hybrid")

#: Modes that cannot run without a supplied proposal.
SEEDED_MODES = ("review", "hybrid")

#: Modes a proposal may be given to. `consult` is happy either way: with one it is a
#: fast critique, without one it is "how would you approach this". `independent` is
#: absent on purpose — a proposal it would never show anyone is a file written to
#: disk claiming a review that never happened.
ACCEPTS_SEED = SEEDED_MODES + ("consult",)

#: Live text is batched before it is logged: enough to feel immediate, few enough
#: records that a long turn does not bury the log in one-word events.
STREAM_FLUSH_CHARS = 120
STREAM_FLUSH_SECONDS = 0.4

#: Who can have set the task. Not a permission — the CLI, the browser and the main
#: agent all reach the same endpoint and none of them is privileged — only a label,
#: so the seat at the head of the table can say whose it is.
CONVENERS = ("user", "agent")


class CouncilError(Exception):
    """The session cannot continue (e.g. too few panelists left)."""


@dataclass
class SessionPaths:
    root: Path

    @property
    def task(self) -> Path:
        return self.root / "task.md"

    @property
    def seed(self) -> Path:
        return self.root / "seed.md"

    @property
    def context(self) -> Path:
        return self.root / "context.md"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def calls_dir(self) -> Path:
        """Raw console logs, one per harness process. Created on first write rather
        than by `prepare()`: an empty `calls/` would claim a capture that is off."""
        return self.root / "calls"

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
    def stream(self) -> Path:
        return self.root / "stream.jsonl"

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


class _Streamer:
    """Coalesces one turn's deltas into log records.

    Text is buffered until it is worth a record; a tool call or the end of the turn
    flushes whatever is pending, so the ordering a reader sees matches what happened.
    """

    def __init__(self, log: EventLog, agent: str, round_no: int, phase: int) -> None:
        self.log = log
        self.agent = agent
        self.round = round_no
        self.phase = phase
        self.session_id: str | None = None
        self.tokens = 0
        self._buffer: list[str] = []
        self._size = 0
        self._last = time.monotonic()

    def __call__(self, delta: Delta) -> None:
        if delta.kind == "text":
            self._buffer.append(delta.text)
            self._size += len(delta.text)
            now = time.monotonic()
            if self._size >= STREAM_FLUSH_CHARS or now - self._last >= STREAM_FLUSH_SECONDS:
                self.flush()
            return

        self.flush()
        if delta.kind == "session":
            # Captured, not logged: it is the same id `turn_end` records, and knowing
            # it early means a turn that later times out can still be resumed.
            self.session_id = delta.session_id or self.session_id
        elif delta.kind == "usage":
            self.tokens += delta.tokens or 0
            self._emit(kind="usage", tokens=self.tokens)
        elif delta.kind == "tool":
            self._emit(kind="tool", tool=delta.tool, target=delta.target)
        elif delta.kind == "status":
            self._emit(kind="status", text=delta.text)

    def flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._size = 0
        self._last = time.monotonic()
        self._emit(kind="text", text=text)

    def _emit(self, **payload) -> None:
        self.log.emit(
            "turn_delta",
            verbose=True,
            agent=self.agent,
            round=self.round,
            phase=self.phase,
            **payload,
        )


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
        mode: str = "independent",
        log: EventLog | None = None,
        controller: Controller | None = None,
        session_id: str | None = None,
        call_gate: asyncio.Semaphore | None = None,
        convened_by: str = "user",
    ):
        if mode not in MODES:
            raise CouncilError(f"unknown mode '{mode}'. Known: {', '.join(MODES)}")
        self.config = config
        #: "agent" or "user". See the `session_created` payload.
        self.convened_by = convened_by if convened_by in CONVENERS else "user"
        self.panel = list(panel)
        self.paths = paths
        self.project_dir = Path(project_dir)
        self.prompts_dir = Path(prompts_dir or PROMPTS_DIR)
        self.opencode_config = opencode_config
        self.scenario_path = scenario_path
        self.progress = progress or (lambda msg: None)
        self.mode = mode
        self.session_id = session_id or paths.root.name
        # Shared across every session the daemon runs: four panelists exploring in
        # parallel, times several sessions, is a lot of processes on one laptop.
        self.call_gate = call_gate

        self.transcript = Transcript()
        self.task_text = ""
        self.seed_text = ""
        self.context_text = ""
        self.tokens_used = 0
        self.dropped: list[tuple[str, str]] = []
        self.started = time.monotonic()
        self.log = log or EventLog(paths.root)
        self.controller = controller or Controller()
        self.controller.on_command = self._log_command
        # So a control naming a panelist that does not exist is refused at the
        # API, rather than accepted with a 200 and dropped on the floor here.
        self.controller.labels = {p.label for p in self.panel}
        self._bench: list[Panelist] = []  # dropped, but restorable by the user
        #: Consecutive failed turns per panelist, reset by any turn that lands.
        self._failures: dict[str, int] = {}
        #: Panelists whose session has already been abandoned once. A conversation
        #: goes bad once; retrying every round just doubles the bill.
        self._cold_retried: set[str] = set()
        #: Numbers the console logs. Across the session, not per turn — see `_new_call_log`.
        self._call_seq = 0
        self._adapters = adapters or self._build_adapters()
        self._templates = {
            name: (self.prompts_dir / f"{name}.md").read_text(encoding="utf-8")
            for name in (
                "independent_plan",
                "discussion_turn",
                "discussion_turn_session",
                "review_turn",
                "consult_turn",
                "compaction",
            )
        }
        # Session continuity: each panelist keeps talking in the harness conversation
        # where it explored the repo, so it does not lose that context between rounds.
        self.sessions: dict[str, str] = {}
        self._seen: dict[str, int] = {}  # transcript turns already shown to each agent
        self._shown: dict[str, set[str]] = {}  # material blocks already delivered

    # ---- infrastructure --------------------------------------------------

    def _build_adapters(self) -> dict[str, Adapter]:
        adapters: dict[str, Adapter] = {}
        for p in self.panel:
            kwargs: dict = {"model": p.model, "variant": p.variant, "effort": p.effort}
            # Only when set: every adapter defaults its own binary name, and passing
            # None would override that default with nothing.
            if p.binary:
                kwargs["binary"] = p.binary
            if p.adapter == "opencode_cli" and self.opencode_config:
                kwargs["config_path"] = self.opencode_config
            if p.adapter == "mock":
                kwargs["panelist_name"] = p.name
                kwargs["scenario_path"] = self.scenario_path
            adapters[p.label] = build_adapter(p.adapter, **kwargs)
        return adapters

    def _event(self, event_kind: str, /, **payload) -> None:
        self.log.emit(event_kind, **payload)

    def _log_command(self, record: dict) -> None:
        self._event("control", **record)
        self.progress(f"  · control: {record['action']} ({record.get('detail')})")

    def _status(self, state: str, **payload) -> None:
        # `pid` lets a reader tell a live run from a corpse: a status still saying
        # "running" whose pid is dead (os.kill(pid, 0) raises) was interrupted, not
        # progressing. `updated_at` is the heartbeat that backs that up.
        record = {
            "state": state,
            "id": self.session_id,
            "mode": self.mode,
            "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(time.monotonic() - self.started, 1),
            "tokens": self.tokens_used,
            "paused": self.controller.paused,
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

    def _new_call_log(
        self, panelist: Panelist, round_no: int, phase: int, session: str | None
    ) -> CallLog | None:
        """A console log for one harness process, or None when capture is off.

        Numbered across the session rather than per turn: a resumed call whose session
        is refused falls back to a cold one for the same round, and naming both after
        the round would have one overwrite the other — losing the failure that caused
        the retry, which is the half worth keeping.
        """
        if not self.config.capture_console:
            return None
        self._call_seq += 1
        name = call_filename(self._call_seq, panelist.label, round_no)
        return CallLog(
            self.paths.calls_dir / name,
            agent=panelist.label,
            phase=phase,
            round_no=round_no,
            model=panelist.model,
            effort=panelist.effort,
            session=session,
            # Announced when it opens, not when it closes. A Phase 1 call can run for
            # fifteen minutes, and a log that only appears afterwards is missing for
            # exactly the stretch anybody would have opened it during.
            on_open=lambda: self._event(
                "call_logged",
                agent=panelist.label,
                round=round_no,
                phase=phase,
                file=name,
                done=False,
            ),
        )

    async def _ask(
        self,
        panelist: Panelist,
        prompt: str,
        timeout: int,
        use_session: bool = True,
        streamer: _Streamer | None = None,
        round_no: int = 0,
        phase: int = 2,
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
        if streamer is not None:
            round_no, phase = streamer.round, streamer.phase
        call_log = self._new_call_log(panelist, round_no, phase, session)
        started = time.monotonic()
        try:
            if self.call_gate is not None:
                async with self.call_gate:
                    reply = await adapter.ask(
                        prompt,
                        cwd=str(self.project_dir),
                        timeout=timeout,
                        session=session,
                        on_delta=streamer,
                        call_log=call_log,
                    )
            else:
                reply = await adapter.ask(
                    prompt,
                    cwd=str(self.project_dir),
                    timeout=timeout,
                    session=session,
                    on_delta=streamer,
                    call_log=call_log,
                )
        except AdapterError as exc:
            reply = Reply(ok=False, error=str(exc))
        except asyncio.CancelledError:
            # A hard stop still leaves a finished file behind. Without this the only
            # event about it was the one emitted when it opened, saying `done=False`,
            # so the log of the call that was interrupted — the one worth reading —
            # stayed marked as running for the life of the session, and the UI polled
            # a file that would never change again.
            self._note_call(
                call_log,
                Reply(ok=False, error="cancelled"),
                round_no,
                phase,
                time.monotonic() - started,
            )
            raise  # a stop, not a failure — must not be swallowed
        except Exception as exc:  # noqa: BLE001
            # One panelist's harness misbehaving is a failed turn, which this protocol
            # already knows how to survive. Letting it out killed the whole council and
            # left status.json still claiming to be running, because only CouncilError
            # reaches the handler that writes "failed".
            reply = Reply(ok=False, error=f"{type(exc).__name__}: {exc}")
        self._note_call(call_log, reply, round_no, phase, time.monotonic() - started)
        if streamer is not None:
            streamer.flush()
            # A timed-out call still opened a conversation; keeping its id means the
            # next turn resumes rather than paying for a full reassembly.
            if not reply.session_id and streamer.session_id:
                reply.session_id = streamer.session_id
        self._account(prompt, reply)
        if use_session and reply.session_id:
            self.sessions[panelist.label] = reply.session_id
        return reply

    def _note_call(
        self,
        call_log: CallLog | None,
        reply: Reply,
        round_no: int,
        phase: int,
        seconds: float,
    ) -> None:
        """Close a console log, and say how the call it recorded turned out.

        The second event for this file — the first went out when it opened. Folding is
        by filename, so this replaces that entry rather than adding one.

        Only when one was actually written: an adapter that raised before starting the
        process leaves no file, and an event pointing at a missing one is worse than
        no event.
        """
        if call_log is None or not call_log.written:
            return
        # Belt and braces. Every path through `run_process` finishes the log, but an
        # adapter that returns early after starting one would otherwise leave it open
        # and footerless.
        call_log.finish(reply.exit_code, seconds, reply.error)
        # A clean exit is not a usable turn. Every adapter rejects a harness that exits
        # 0 having produced nothing parseable — and by the time it does, this file is
        # closed saying `exit code: 0` with no error. True of the process, false about
        # the call, and nothing in the log said so.
        if not reply.ok and reply.error:
            call_log.note("rejected", reply.error)
        self._event(
            "call_logged",
            agent=call_log.agent,
            round=round_no,
            phase=phase,
            file=call_log.path.name,
            seconds=round(seconds, 1),
            exit_code=reply.exit_code,
            bytes=call_log.bytes_seen,
            truncated=call_log.truncated,
            ok=reply.ok,
            done=True,
        )

    def _heartbeat(self) -> None:
        """Running totals, at every turn boundary.

        status.json is only rewritten when the phase or round changes, which in a real
        session is minutes apart — far too coarse for a live counter. This is the same
        two numbers, emitted often and cheaply, and it is what the UI actually reads.
        """
        self.log.emit(
            "heartbeat",
            verbose=True,
            elapsed=round(time.monotonic() - self.started, 1),
            tokens=self.tokens_used,
        )

    def _log_prompt(self, panelist: Panelist, prompt: str, round_no: int, phase: int) -> None:
        self.log.emit(
            "prompt",
            verbose=True,
            agent=panelist.label,
            round=round_no,
            phase=phase,
            text=prompt,
        )

    def _has_session(self, panelist: Panelist) -> bool:
        return bool(
            self.config.protocol.session_continuity
            and self.sessions.get(panelist.label)
        )

    def _mark_caught_up(self, panelist: Panelist) -> None:
        """Record what this panelist has now been shown, so later turns send only deltas."""
        self._seen[panelist.label] = len(self.transcript.turns)
        self._shown.setdefault(panelist.label, set()).update(
            {"plans", "proposal", "context"}
        )

    def _drop(self, panelist: Panelist, why: str) -> None:
        self.panel = [p for p in self.panel if p.label != panelist.label]
        if all(p.label != panelist.label for p in self._bench):
            self._bench.append(panelist)
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
        # utf-8-sig: Notepad writes a byte-order mark by default, and it would
        # otherwise be the first character of every panelist's task prompt.
        self.task_text = self.paths.task.read_text(encoding="utf-8-sig")
        if not self.task_text.strip():
            raise CouncilError(
                "the task is empty. It is the only thing the panel is told, so there "
                "is nothing for it to plan."
            )
        if self.paths.seed.is_file():
            self.seed_text = self.paths.seed.read_text(encoding="utf-8-sig")
        if self.mode in SEEDED_MODES and not self.seed_text.strip():
            raise CouncilError(f"mode '{self.mode}' needs a proposal, but seed.md is empty")
        if self.paths.context.is_file():
            self.context_text = self.paths.context.read_text(encoding="utf-8-sig")

        self._event(
            "session_created",
            schema=SCHEMA_VERSION,
            id=self.session_id,
            mode=self.mode,
            project_dir=str(self.project_dir),
            started_at=datetime.now(timezone.utc).isoformat(),
            identities=identity_map(self.panel),
            panel=[
                # `effort` belongs here with the model: it is the other half of what was
                # actually run, and the difference between `low` and `max` on the same
                # panel is an order of magnitude of tokens. A log that records which
                # model answered but not how hard it was told to think cannot explain
                # why two runs of the same council cost what they did.
                {
                    "label": p.label,
                    "name": p.name,
                    "model": p.model,
                    "adapter": p.adapter,
                    "effort": p.effort,
                }
                for p in self.panel
            ],
            protocol=vars(self.config.protocol),
            timeouts=vars(self.config.timeouts),
            on_failure=self.config.on_failure,
            # Who set the task — the main agent working in an editor, or a person at a
            # terminal or in the browser. It is a self-declaration, not an authenticated
            # fact, and it exists so the chair at the head of the table can be named
            # correctly rather than to grant anybody anything.
            convened_by=self.convened_by,
            task_chars=len(self.task_text),
            seed_chars=len(self.seed_text),
            context_chars=len(self.context_text),
        )
        try:
            if self.mode in PLANNING_MODES:
                await self.phase1()
            else:
                self.progress(
                    "Phase 1 skipped: the panel starts from what it was given — "
                    + ("the brief." if self.mode == "consult" else "the proposal.")
                )
            rounds, termination = await self.phase2()
            self.phase3(rounds, termination)
        except CouncilError as exc:
            # Write down whatever was said before failing. Losing quorum used to
            # discard the transcript and the digest for rounds that really happened
            # and were really paid for — the panel argued for ten minutes and left
            # nothing behind but an error. The session still fails; it is just no
            # longer amnesiac about it.
            self._salvage(str(exc))
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
        self._event("phase_start", phase=1)
        # No brief here, ever. See `_context_block` — this is the prompt whose
        # independence the whole feature is arranged around, and the omission is the
        # feature. tests/test_context_and_consult.py asserts it.
        prompt = self._templates["independent_plan"].replace("{task}", self.task_text)
        timeout = self.config.timeouts.per_call_phase1

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
        self._event("turn_start", agent=panelist.label, phase=1, round=0,
                    prompt_chars=len(prompt))
        self._log_prompt(panelist, prompt, round_no=0, phase=1)
        streamer = _Streamer(self.log, panelist.label, 0, 1)
        reply = await self._ask(panelist, prompt, timeout, streamer=streamer)
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
        else:
            self._event(
                "turn_failed", agent=panelist.label, round=0,
                error=reply.error or "empty plan",
            )
        self._heartbeat()
        return panelist.label, reply

    # ---- the opening round of a consultation -----------------------------

    def _opening_round(self, round_no: int) -> bool:
        """Whether this round is answered in parallel rather than round-robin.

        Only ever round 1 of a `consult`, and it is that mode's whole shape: instead of
        each panelist writing a plan nobody asked for, each gives its own reading of the
        brief, at the same time and without seeing the others. It is Phase 1's
        independence bought at Phase 1's price — one call — and everything after it is an
        ordinary debate.
        """
        return self.mode == "consult" and round_no == 1

    async def _round_in_parallel(self, round_no: int, expected: list[str]) -> list[str]:
        """Everyone answers at once. Returns who was still due to speak.

        Calls run concurrently; results are committed in panel order. The events land as
        each panelist finishes — that is when it really happened, and it is what makes
        the live view live — but transcript.md and the digest must not be ordered by
        which model happened to be quickest that afternoon.
        """
        panel = []
        for panelist in list(self.panel):
            if self.controller.should_skip(panelist.label):
                self.controller.clear_skip(panelist.label)
                self._event("turn_skipped", agent=panelist.label, round=round_no)
                self.progress(f"    {panelist.label}: skipped by the user")
                expected = [lbl for lbl in expected if lbl != panelist.label]
                continue
            panel.append(panelist)

        # How many turns existed before this round: what each of these panelists had in
        # its prompt, and therefore all it may be considered to have seen.
        before = len(self.transcript.turns)
        # Phase 1's timeout, not the discussion turn's: these panelists are reading the
        # repository cold, which is the expensive half of the call.
        timeout = self.config.timeouts.per_call_phase1
        tasks = [
            asyncio.create_task(self._ask_in_parallel(p, round_no, timeout)) for p in panel
        ]
        answers: dict[str, tuple[Reply, Envelope | None]] = {}
        for coro in asyncio.as_completed(tasks):
            label, reply, envelope = await coro
            answers[label] = (reply, envelope)

        for panelist in panel:
            reply, envelope = answers[panelist.label]
            if envelope is None:
                self._record_failure(panelist, round_no, reply.error or "no response")
                expected = [lbl for lbl in expected if lbl != panelist.label]
                continue
            self.transcript.add(Turn.from_envelope(round_no, panelist.label, envelope))
            self._failures.pop(panelist.label, None)
            # `_shown` is already right: `_cold_prompt` built every one of these prompts.
            # `_seen` is not, and deliberately not `_mark_caught_up`, which records "you
            # have seen every turn so far" — true after a round-robin turn and false
            # after this one. These turns ran at the same time as each other, so none of
            # them was in anyone's prompt; marking them seen would drop all of them from
            # round 2 and leave a panel that never heard a word the others said.
            self._seen[panelist.label] = before
        return expected

    async def _ask_in_parallel(self, panelist: Panelist, round_no: int, timeout: int):
        """One panelist's turn, logged the moment it lands rather than in turn order."""
        prompt = self._cold_prompt(panelist, round_no)
        self._event(
            "turn_start", agent=panelist.label, phase=2, round=round_no,
            resumed=False, prompt_chars=len(prompt),
        )
        self._log_prompt(panelist, prompt, round_no, phase=2)
        streamer = _Streamer(self.log, panelist.label, round_no, 2)
        reply = await self._ask(panelist, prompt, timeout, streamer=streamer)

        if not (reply.ok and reply.text.strip()):
            note = reply.error or "no response"
            self._event("turn_failed", agent=panelist.label, round=round_no, error=note)
            self.progress(f"    ! {panelist.label} failed: {note[:120]}")
            self._heartbeat()
            return panelist.label, reply, None

        envelope = parse_envelope(reply.text)
        self._event(
            "turn_end",
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
            resumed=False,
        )
        self._heartbeat()
        flag = " (malformed envelope)" if envelope.malformed else ""
        self.progress(
            f"    {panelist.label}: {envelope.verdict}{flag} ({reply.duration:.0f}s)"
        )
        return panelist.label, reply, envelope

    async def phase2(self) -> tuple[int, str]:
        protocol = self.config.protocol
        self.progress(
            f"Phase 2: discussion (min {protocol.min_rounds}, max {protocol.max_rounds} rounds)"
        )
        self._event("phase_start", phase=2)
        round_no = 0

        while round_no < protocol.max_rounds:
            self._apply_pending(round_no)
            halt = self._halt_requested()
            if halt:
                return round_no, halt
            await self.controller.gate()

            round_no += 1
            self.progress(
                f"  Round {round_no}:"
                + (
                    " everyone at once, nobody seeing the others"
                    if self._opening_round(round_no)
                    else ""
                )
            )
            self._status("running", phase=2, round=round_no)
            self._event("round_start", round=round_no, panel=[p.label for p in self.panel])

            expected = [p.label for p in self.panel]
            if self._opening_round(round_no):
                # No per-turn gate here: the calls are all in flight at once, so there is
                # no boundary between them to pause at. The round boundary above is it.
                expected = await self._round_in_parallel(round_no, expected)
                self._require_quorum()
                budget = self._budget_exceeded()
                if budget:
                    self._event("terminated", reason=budget, round=round_no)
                    return round_no, budget
            else:
                for panelist in list(self.panel):
                    await self.controller.gate()
                    self._apply_pending(round_no)
                    halt = self._halt_requested()
                    if halt:
                        return round_no, halt
                    if panelist.label not in {p.label for p in self.panel}:
                        continue  # dropped earlier in this same round
                    if self.controller.should_skip(panelist.label):
                        self.controller.clear_skip(panelist.label)
                        self._event("turn_skipped", agent=panelist.label, round=round_no)
                        self.progress(f"    {panelist.label}: skipped by the user")
                        expected = [lbl for lbl in expected if lbl != panelist.label]
                        continue

                    await self._take_turn(panelist, round_no)
                    self._require_quorum()

                    budget = self._budget_exceeded()
                    if budget:
                        self._event("terminated", reason=budget, round=round_no)
                        return round_no, budget

            expected = [lbl for lbl in expected if lbl in {p.label for p in self.panel}]
            if expected and self.transcript.all_ready(round_no, expected):
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
        return round_no, "max_rounds"

    def _halt_requested(self) -> str | None:
        if self.controller.stop:
            self._event("terminated", reason="stopped", round=self.transcript.rounds_held)
            return "stopped"
        if self.controller.digest_now:
            self.controller.digest_now = False
            self._event(
                "terminated", reason="digest_requested", round=self.transcript.rounds_held
            )
            return "digest_requested"
        return None

    def _apply_pending(self, round_no: int) -> None:
        """Fold in whatever the user asked for since the last turn boundary."""
        for key, value in self.controller.take_extensions().items():
            setattr(self.config.protocol, key, value)
            self._event("budget_extended", field=key, value=value)
            self.progress(f"  · {key} raised to {value}")

        for label in self.controller.take_drops():
            panelist = next((p for p in self.panel if p.label == label), None)
            if panelist is None:
                continue
            if len(self.panel) <= MIN_PANEL:
                # Refused rather than obeyed: dropping through the floor here was not
                # checked at all, so a user could empty the panel and the council ran
                # on to produce a digest declaring 0 panelists unanimously READY.
                self._event(
                    "control_refused",
                    action="drop",
                    agent=label,
                    reason=f"a council needs at least {MIN_PANEL} panelists",
                )
                self.progress(
                    f"  · refused to drop {label}: a council needs at least "
                    f"{MIN_PANEL} panelists. Stop the session instead."
                )
                continue
            self._drop(panelist, "dropped by you")

        for label in self.controller.take_restores():
            panelist = next((p for p in self._bench if p.label == label), None)
            if panelist is None or any(p.label == label for p in self.panel):
                continue
            self._bench = [p for p in self._bench if p.label != label]
            self.dropped = [(lbl, why) for lbl, why in self.dropped if lbl != label]
            self.panel.append(panelist)
            self.panel.sort(key=lambda p: p.letter)
            self._event("panelist_restored", agent=label)
            self.progress(f"  · {label} returned to the panel")

        for text, by in self.controller.take_chair():
            turn = Turn.chair(max(round_no, 1), text, by)
            self.transcript.add(turn)
            self._event("chair_message", round=turn.round, text=text, by=by)
            self.progress(f"  · chair message from {by}")

    def _worth_retrying_cold(self, panelist: Panelist, reply: Reply) -> bool:
        """Whether a failed resumed turn should be retried without its session.

        This used to fire for every failure, which doubled the cost of each one and
        mislabelled it: a rate limit, an expired login and a wedged harness all came
        out as "session unusable, retrying cold". A timeout is the expensive case —
        the retry buys a second full `per_call` — and it is the one where the session
        is least likely to be the problem, because the harness was plainly alive.

        Once per panelist, too: a session goes bad once. Retrying every round meant a
        panelist whose harness was simply broken paid double for the whole run.
        """
        error = (reply.error or "").lower()
        if "timed out" in error:
            return False
        if panelist.label in self._cold_retried:
            return False
        self._cold_retried.add(panelist.label)
        return True

    def _record_failure(self, panelist: Panelist, round_no: int, note: str) -> None:
        """What a failed turn means, for whichever path produced it.

        The event has already been emitted by the caller — it belongs to the moment the
        call came back, and in a parallel round that is not the moment this runs. What is
        here is only the policy, which must be the same either way.
        """
        self.transcript.add(Turn.failure(round_no, panelist.label, note))
        if self.config.on_failure == "abort":
            raise CouncilError(
                f"{panelist.label} failed and on_failure is 'abort', so the council "
                f"stopped: {note}. Set on_failure: skip_with_note in council.yaml to "
                "carry on with the panelists that are still answering."
            )
        # `skip_with_note` means what it says. One failure used to remove the model
        # from every later round, so two unrelated timeouts on a three-seat panel
        # ended the session — while the name, the config comment and the README all
        # promised it would merely sit the round out.
        self._failures[panelist.label] = self._failures.get(panelist.label, 0) + 1
        if self._failures[panelist.label] >= CONSECUTIVE_FAILURES_BEFORE_DROP:
            self._drop(
                panelist,
                f"{self._failures[panelist.label]} turns in a row failed; last: {note}",
            )

    async def _take_turn(self, panelist: Panelist, round_no: int) -> None:
        resumed = self._has_session(panelist)
        prompt = (
            self._session_prompt(panelist, round_no)
            if resumed
            else self._cold_prompt(panelist, round_no)
        )
        self._event(
            "turn_start", agent=panelist.label, phase=2, round=round_no,
            resumed=resumed, prompt_chars=len(prompt),
        )
        self._log_prompt(panelist, prompt, round_no, phase=2)
        streamer = _Streamer(self.log, panelist.label, round_no, 2)
        reply = await self._ask(
            panelist, prompt, self.config.timeouts.per_call, streamer=streamer
        )

        if not reply.ok and resumed and self._worth_retrying_cold(panelist, reply):
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
            prompt = self._cold_prompt(panelist, round_no)
            self._log_prompt(panelist, prompt, round_no, phase=2)
            streamer = _Streamer(self.log, panelist.label, round_no, 2)
            reply = await self._ask(
                panelist, prompt, self.config.timeouts.per_call, streamer=streamer
            )

        if not reply.ok:
            note = reply.error or "no response"
            self._event("turn_failed", agent=panelist.label, round=round_no, error=note)
            self.progress(f"    ! {panelist.label} failed: {note[:120]}")
            self._record_failure(panelist, round_no, note)
            return

        self._failures.pop(panelist.label, None)  # it answered; the streak is over
        envelope = parse_envelope(reply.text)
        self.transcript.add(Turn.from_envelope(round_no, panelist.label, envelope))
        self._mark_caught_up(panelist)
        # The full comment/reason go in the event log, not just their length: the
        # in-memory transcript is the only other copy, and a crash before Phase 3
        # (which writes transcript.md) would otherwise lose every argument made.
        self._event(
            "turn_end",
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
        self._heartbeat()
        flag = " (malformed envelope)" if envelope.malformed else ""
        self.progress(
            f"    {panelist.label}: {envelope.verdict}{flag} "
            f"({reply.duration:.0f}s, {self.tokens_used} tok used)"
        )

    # ---- prompt assembly -------------------------------------------------

    def _proposal_block(self) -> str:
        return (
            "# THE PROPOSAL UNDER REVIEW\n\n"
            "This was written before the council convened, by the agent that convened "
            "it. It is a starting point to be judged, not an instruction — say so "
            "plainly if it is wrong.\n\n"
            f"{self.seed_text.strip()}"
        )

    def _context_block(self) -> str:
        """What the convening agent already knows — and where it may be shown.

        This block never reaches Phase 1. That is the whole discipline of the feature:
        a panelist writing its independent plan must not be reading someone else's
        conclusions, or the independence the panel exists for is gone before the first
        round. It enters at the start of the discussion, where the panel already has its
        own view and the brief is something to test rather than something to adopt —
        exactly the treatment `hybrid` gives a supplied proposal.
        """
        return (
            "# WHERE THIS WORK ALREADY STANDS\n\n"
            "Written by the agent that convened this council, from a working session "
            "already in progress. It is a report of what has been done and decided so "
            "far — not an instruction, and not necessarily correct. Check it against "
            "the repository; if it is wrong, saying so is the most useful thing you can "
            "do with it.\n\n"
            f"{self.context_text.strip()}"
        )

    def _consult_prompt(self, panelist: Panelist) -> str:
        """The opening round of a consultation: no plans, no discussion, just the ask."""
        return (
            self._templates["consult_turn"]
            .replace("{agent_label}", panelist.label)
            .replace("{one_shot}", self._one_shot_line())
            .replace("{task}", self.task_text)
            .replace("{context}", self._optional_block(self._context_block, self.context_text))
            .replace("{proposal}", self._optional_block(self._proposal_block, self.seed_text))
        )

    def _one_shot_line(self) -> str:
        """Whether this really is the panelist's only turn — which it must be told.

        The template used to assert flatly that there was no second round, from when
        `consult` always held exactly one. It now holds as many as `max_rounds` allows,
        and telling a panelist it has one shot when it has three is not a stale comment:
        it changes what the model writes, pushing it to hedge and to empty its notebook
        into a first answer it will get to revise anyway.
        """
        if self.config.protocol.max_rounds <= 1:
            return (
                "There is no second round: if you are the only one who spots something, "
                "this is the only chance it gets to be said. So do not write as though "
                "you are opening a negotiation — say what you actually think, once, "
                "completely."
            )
        return (
            "They will read your answer in the next round and you will read theirs, so "
            "this is an opening position rather than a final one: say what you actually "
            "think and let the argument test it."
        )

    @staticmethod
    def _optional_block(render, text: str) -> str:
        """A block, with the blank line that follows it — or nothing at all."""
        return render() + "\n\n" if text.strip() else ""

    def _session_prompt(self, panelist: Panelist, round_no: int) -> str:
        """Only what this panelist has not seen — its own context lives in its session."""
        seen = self._shown.setdefault(panelist.label, set())
        blocks: list[str] = []

        # First round of the discussion for this panelist, and the first moment the
        # brief is allowed to exist as far as it is concerned.
        if self.context_text.strip() and "context" not in seen:
            seen.add("context")
            blocks.append(self._context_block())

        if self.seed_text.strip() and "proposal" not in seen:
            seen.add("proposal")
            blocks.append(self._proposal_block())

        if "plans" not in seen:
            seen.add("plans")
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

        # Never a panelist's own words: they are already in the session this prompt is
        # continuing. A round-robin turn gets this for free, because `_seen` advances
        # past it the moment it lands. A parallel round cannot — `_seen` has to stay
        # behind all of that round's turns so the *others* still arrive — so the rule is
        # stated here instead of falling out of the arithmetic.
        new_turns = [
            turn
            for turn in self.transcript.turns[self._seen.get(panelist.label, 0) :]
            if turn.label != panelist.label
        ]
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

    def _cold_prompt(self, panelist: Panelist, round_no: int) -> str:
        """A full reassembly: everything this panelist needs, assuming it remembers nothing."""
        self._shown.setdefault(panelist.label, set()).update(
            {"plans", "proposal", "context"}
        )
        if self._opening_round(round_no):
            return self._consult_prompt(panelist)
        context = self._optional_block(self._context_block, self.context_text)
        if self.mode == "review":
            return (
                self._templates["review_turn"]
                .replace("{agent_label}", panelist.label)
                .replace("{task}", self.task_text)
                .replace("{context}", context)
                .replace("{proposal}", self.seed_text.strip())
                .replace("{conversation}", self.transcript.render_conversation())
                .replace("{round_no}", str(round_no))
            )
        return (
            self._templates["discussion_turn"]
            .replace("{agent_label}", panelist.label)
            .replace("{origin}", self._origin_line())
            .replace("{task}", self.task_text)
            .replace("{context}", context)
            .replace("{proposal}", self._optional_block(self._proposal_block, self.seed_text))
            .replace("{plans}", self.transcript.render_plans(panelist))
            .replace("{conversation}", self.transcript.render_conversation())
            .replace("{round_no}", str(round_no))
        )

    def _origin_line(self) -> str:
        """How this panel came to be arguing — which is not the same in every mode.

        The template used to assert that everyone had written an independent plan. In a
        consultation nobody has, and a prompt that tells a model it produced something it
        did not is an invitation to invent it.
        """
        if self.transcript.plans:
            return (
                "Each of you wrote an independent plan without seeing the others. Now "
                "you are working towards a single plan that is better than any one of "
                "them."
            )
        if self.mode == "consult":
            return (
                "Each of you gave your own reading of the situation below, at the same "
                "time and without seeing the others. Now you are working towards a "
                "single answer that is better than any one of them."
            )
        return (
            "You are reviewing the proposal below together, and working towards a single "
            "verdict on it that is better than any one of yours alone."
        )

    # ---- budgets and compaction ------------------------------------------

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
        self._event(
            "compaction_start", agent=panelist.label,
            through_round=rounds[-1], from_round=rounds[0],
        )
        # Cold call on purpose: a summarising request must not be injected into the
        # panelist's own debate session, where it would pollute its context.
        reply = await self._ask(
            panelist,
            prompt,
            self.config.timeouts.per_call,
            use_session=False,
            round_no=rounds[-1],
        )
        summary = reply.text.strip() if reply.ok else ""
        problem = _unusable_summary(summary)
        if problem:
            # Compaction is an optimisation; losing it costs tokens, not correctness —
            # but *accepting a bad one* costs correctness, because those rounds then
            # reach every later prompt only as whatever this reply said. Any non-blank
            # answer used to qualify, so a refusal, a one-word reply, or the
            # compactor's own envelope replaced the discussion and nothing showed it:
            # transcript.md renders all turns regardless, so the damage was invisible.
            self._event("compaction_failed", error=reply.error or problem)
            self.progress(f"  · compaction skipped: {problem}")
            return
        self.transcript.apply_compaction(summary, rounds[-1])
        self._event("compacted", through_round=rounds[-1], agent=panelist.label)

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

    # ---- artefacts -------------------------------------------------------

    def phase3(self, rounds: int, termination: str) -> None:
        self.progress("Phase 3: writing transcript and digest...")
        self._event("phase_start", phase=3)
        header = (
            f"Panel of {len(self.panel)} · {rounds} round(s) · "
            f"mode: {self.mode} · "
            f"ended: {TERMINATION_LABELS.get(termination, termination)}"
        )
        self.paths.transcript.write_text(
            self.transcript.render_transcript_md(header), encoding="utf-8"
        )
        self.paths.digest.write_text(
            self.render_digest(rounds, termination), encoding="utf-8"
        )

    def _salvage(self, error: str) -> None:
        """Write the transcript and digest for a session that is about to fail.

        Best-effort by construction: this runs on the way out of a failure, so an
        exception here would replace a diagnosable error with a confusing one.
        """
        if not self.transcript.turns or self.paths.digest.is_file():
            return
        try:
            self.phase3(self.transcript.rounds_held, "aborted")
            self.progress(f"  · wrote a partial digest: {self.paths.digest}")
        except Exception:  # noqa: BLE001 - never mask the real failure
            pass

    def render_digest(self, rounds: int, termination: str) -> str:
        reviewing = self.mode == "review"
        consulting = self.mode == "consult"
        #: A consultation's opening round is answered in parallel, so if that is all it
        #: held then no panelist ever heard another. One more round and it is an ordinary
        #: debate — which is why this reads the round count rather than the mode.
        unheard = consulting and rounds <= 1
        lines = [
            "# Council digest",
            "",
            f"- **Panelists:** {len(self.panel)} of {len(self.panel) + len(self._bench)} "
            f"still seated ({', '.join(p.label for p in self.panel) or 'none'})",
            f"- **Mode:** {MODE_LABELS.get(self.mode, self.mode)}",
            f"- **Rounds held:** {rounds}",
            f"- **Ended because:** {TERMINATION_LABELS.get(termination, termination)}",
        ]
        if self.dropped:
            dropped = "; ".join(f"{lbl} ({why[:80]})" for lbl, why in self.dropped)
            lines.append(f"- **Dropped mid-session:** {dropped}")
        chair = [t for t in self.transcript.turns if t.kind == "chair"]
        if chair:
            lines.append(
                f"- **Interventions:** {len(chair)} chair message(s) steered this "
                "discussion; see the transcript."
            )
        if self.seed_text.strip():
            lines.append(
                "- **Note:** the panel started from a supplied proposal, so its "
                "framing shaped the discussion."
            )
        if self.context_text.strip():
            # Which half of this sentence is true decides how much of the panel's
            # agreement is its own. Worth spelling out: the digest is the only artefact
            # anyone reads later, and by then nobody remembers how the run was set up.
            lines.append(
                "- **Note:** the convening agent supplied a context brief. "
                + (
                    "The panel wrote its independent plans before seeing it; it "
                    "entered at the start of the discussion."
                    if self.mode in PLANNING_MODES
                    else "The panel had it from the first word, so it framed everything below."
                )
            )
        if unheard:
            lines.append(
                "- **Note:** this held one round, answered in parallel, so no panelist "
                "saw any other's answer. Where they agree, that is independent agreement "
                "rather than a settled argument; where they disagree, nothing has tested "
                "which of them is right."
            )
        elif consulting:
            lines.append(
                f"- **Note:** the opening round was answered in parallel, each panelist "
                f"without sight of the others; the {rounds - 1} round(s) after it are an "
                "ordinary debate."
            )
        if termination != "all_ready":
            lines.append(
                "- **Note:** the discussion was cut short — positions below may not be final."
            )

        if unheard:
            heading = "## What each panelist said"
        elif reviewing:
            heading = "## Each panelist's verdict on the proposal"
        else:
            heading = "## Final position of each panelist"
        lines += ["", heading, ""]

        unresolved: list[tuple[str, str]] = []
        # Dropped panelists included. A panelist that argued a blocker and then lost
        # one call was removed from `self.panel`, and its objection vanished from the
        # digest — the artefact the implementing agent reads — while the header went
        # on saying every panelist signalled READY. The transcript kept it; the
        # summary silently did not.
        for panelist in sorted(self.panel + self._bench, key=lambda p: p.letter):
            turn = self.transcript.last_turn_of(panelist.label)
            gone = next((why for lbl, why in self.dropped if lbl == panelist.label), None)
            verdict = turn.verdict if turn else "no reply"
            suffix = " · dropped before the end" if gone else ""
            lines.append(f"### {panelist.label} — {verdict}{suffix}")
            lines.append("")
            if gone:
                lines += [f"_Dropped: {gone.strip()}_", ""]
            if turn is None:
                lines += ["_never completed a turn_", ""]
                if gone:
                    unresolved.append((panelist.label, f"dropped before speaking: {gone.strip()}"))
                continue
            if turn.malformed:
                lines += [
                    "_This panelist did not answer in the agreed format; the verdict "
                    "below was inferred from its prose._",
                    "",
                ]
            lines += [turn.comment.strip() or "_(empty)_", ""]
            if turn.reason:
                if turn.verdict == READY:
                    label = "Nothing blocking, because" if consulting else "Why ready"
                else:
                    label = "Blocked on" if consulting else "Still open"
                lines += [f"**{label}:** {turn.reason.strip()}", ""]
            if turn.verdict == CONTINUE or gone:
                why = turn.reason.strip() or (
                    "see their answer above" if unheard else "see final position above"
                )
                unresolved.append(
                    (panelist.label, f"{why} (dropped before the end)" if gone else why)
                )

        if unheard:
            open_heading = "## Concerns raised"
        elif reviewing:
            open_heading = "## Blocking objections to the proposal"
        else:
            open_heading = "## Points still open when the session ended"
        lines += [open_heading, ""]
        if not unresolved:
            lines.append(
                "No panelist raised a blocking concern."
                if unheard
                else "Every panelist ended on READY; no panelist recorded an outstanding "
                "objection."
            )
        else:
            lines.append(
                "These panelists found something that should change the decision. Each "
                "item below is for the user to settle:"
                if unheard
                else "These panelists had not accepted the plan as settled. Each item "
                "below is a decision for the user:"
            )
            lines.append("")
            for label, why in unresolved:
                lines.append(f"- **{label}:** {why}")

        # What is actually on disk, rather than what the mode usually implies: a hybrid
        # session has both a proposal and independent plans, and a consultation has
        # neither, so naming one by mode was wrong twice over.
        # "Full discussion" where no discussion happened. Spotted by a real panelist
        # reviewing this feature; it applies to a one-round consultation and no longer to
        # a consultation that went on to debate.
        artefacts = [
            f"{'Every answer in full' if unheard else 'Full discussion'}: "
            f"`{self.paths.transcript.name}`"
        ]
        if self.seed_text.strip():
            artefacts.append("the proposal: `seed.md`")
        if self.context_text.strip():
            artefacts.append("the brief it was given: `context.md`")
        if self.transcript.plans:
            artefacts.append("independent plans: `plans/`")
        artefacts.append(f"token estimate: {self.tokens_used}")
        lines += ["", "---", "", " · ".join(artefacts), ""]
        return "\n".join(lines)


#: A summary shorter than this is not a summary of several rounds of argument.
MIN_SUMMARY_CHARS = 200


def _unusable_summary(summary: str) -> str:
    """Why this reply cannot stand in for the rounds it claims to summarise, or "".

    The compactor is asked for prose. A verdict envelope means it answered as a
    panelist instead — which happens when the compaction prompt reaches a model mid
    debate — and a refusal or a stub means it did not do the work. Either way the
    rounds would be replaced by something that is not about them.
    """
    if not summary:
        return "the compactor returned nothing"
    if len(summary) < MIN_SUMMARY_CHARS:
        return f"the summary was only {len(summary)} characters"
    if '"verdict"' in summary or "'verdict'" in summary:
        return "the compactor replied with a verdict envelope, not a summary"
    return ""


TERMINATION_LABELS = {
    "all_ready": "every panelist signalled READY",
    "max_rounds": "the round limit was reached",
    "token_budget": "the token budget was reached",
    "wall_clock_budget": "the time budget was reached",
    "stopped": "the user stopped the session",
    "digest_requested": "the user asked for the digest early",
    "aborted": "the session failed before it could finish — this is a partial record",
}

MODE_LABELS = {
    "independent": "independent — every panelist planned from scratch",
    "review": "review — the panel critiqued a supplied proposal",
    "hybrid": "hybrid — independent plans first, then the supplied proposal",
    "consult": "consult — the panel started from a brief, not from plans of its own",
}
