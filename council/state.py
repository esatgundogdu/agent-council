"""The reducer: a session's log folded into the state a client renders.

This is the only place that interprets events. The daemon serves `build_state()` as
the initial snapshot and then streams raw events for the client to fold with the same
rules, so a live view and a reload of a finished session cannot disagree.

Sessions written before the daemon existed (a bare `turn` event, no sequence numbers)
still render: `_legacy_turn` maps them onto the current shape.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .events import read_events

#: A run whose heartbeat is older than this is treated as stalled rather than live.
STALE_AFTER_SECONDS = 900

#: How much streamed text is kept per turn in a snapshot. The full text arrives in
#: `turn_end`; this is only the tail a client shows while the turn is still running.
LIVE_TEXT_LIMIT = 20_000

#: How many "what it just did" lines a running turn keeps. Enough to see movement and
#: where it is looking, few enough that the turn body stays readable.
TRAIL_LIMIT = 40


def find_sessions(project_dir: str | Path) -> list[Path]:
    """Session directories under `.council/`, newest first."""
    root = Path(project_dir) / ".council"
    if not root.is_dir():
        return []
    sessions = [p for p in root.iterdir() if p.is_dir() and (p / "task.md").is_file()]
    return sorted(sessions, key=lambda p: p.name, reverse=True)


def pid_alive(pid: object) -> bool:
    """Whether the process that wrote status.json is still running.

    A pid can be reused, so this is a hint rather than proof; the heartbeat age in
    `build_state` is what stops a corpse from being reported as live.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by someone else
        return True
    except OSError:
        return False
    return True


#: OpenProcess access right that only needs to ask whether a process exists.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def _windows_pid_alive(pid: int) -> bool:
    """Ask Windows directly, because `os.kill(pid, 0)` cannot be used here.

    On Windows CPython implements signal 0 through TerminateProcess, and for a pid
    that has already gone it raises `SystemError: <class 'OSError'> returned a result
    with an exception set` — which no `except OSError` will catch, and which took the
    whole request down when it escaped. OpenProcess answers the actual question.
    """
    import ctypes

    # use_last_error, or GetLastError is whatever some later call left behind and the
    # access-denied case below reads as "no such process".
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means it exists and belongs to someone else; anything else
        # (typically ERROR_INVALID_PARAMETER) means there is no such process.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # it exists; we simply cannot read its exit code
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def age_seconds(timestamp: object) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _round_of(event: dict) -> int:
    """A round number, from an event that may not carry a usable one.

    `int("one")` and `int([1])` both raise, and a single such record made the whole
    session unreadable — every endpoint that folds it answered 500.
    """
    value = event.get("round")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


class Reducer:
    """Folds events into the client-facing shape, one event at a time."""

    def __init__(self) -> None:
        self.meta: dict = {}
        self.identities: dict[str, str] = {}
        self.models: dict[str, str] = {}
        self.efforts: dict[str, str] = {}
        self.adapters: dict[str, str] = {}
        self.order: list[str] = []
        self.plans: dict[str, dict] = {}
        self.rounds: dict[int, list[dict]] = {}
        self.open_turns: dict[str, dict] = {}
        self.activity: dict[str, dict] = {}
        self.trails: dict[str, list[str]] = {}
        self.health: list[dict] = []
        self.controls: list[dict] = []
        self.dropped: dict[str, str] = {}
        self.tokens_by_agent: dict[str, int] = {}
        self.compactions: list[dict] = []
        self.progress: dict = {}
        self.seq = 0

    # ---- fold ------------------------------------------------------------

    def feed(self, event: dict) -> None:
        seq = event.get("seq")
        if isinstance(seq, int):
            self.seq = max(self.seq, seq)
        kind = event.get("event")
        agent = event.get("agent")
        handler = getattr(self, f"_on_{kind}", None) if isinstance(kind, str) else None
        if handler is not None:
            handler(event, agent)

    # session lifecycle ----------------------------------------------------

    def _on_session_created(self, event: dict, _agent) -> None:
        self.meta = {
            k: event.get(k)
            for k in (
                "id", "project_dir", "mode", "protocol", "timeouts",
                "on_failure", "task_chars", "seed_chars", "context_chars",
                "schema", "started_at",
            )
        }
        raw_ids = event.get("identities")
        if isinstance(raw_ids, dict):
            self.identities = {str(k): str(v) for k, v in raw_ids.items()}
        for entry in _as_list(event.get("panel")):
            if not isinstance(entry, dict):
                continue
            label = entry.get("label")
            if not isinstance(label, str):
                continue
            self.order.append(label)
            if entry.get("model"):
                self.models[label] = str(entry["model"])
            if entry.get("effort"):
                self.efforts[label] = str(entry["effort"])
            if entry.get("adapter"):
                self.adapters[label] = str(entry["adapter"])

    #: V1 sessions announced themselves with a different name and no mode.
    _on_session_start = _on_session_created

    def _on_phase_start(self, event: dict, _agent) -> None:
        self.progress["phase"] = event.get("phase")

    def _on_round_start(self, event: dict, _agent) -> None:
        self.progress["round"] = event.get("round")

    def _on_heartbeat(self, event: dict, _agent) -> None:
        self.progress["tokens"] = event.get("tokens")
        self.progress["elapsed"] = event.get("elapsed")

    def _on_session_end(self, event: dict, _agent) -> None:
        self.progress.update(
            rounds=event.get("rounds"),
            termination=event.get("termination"),
            tokens=event.get("tokens"),
        )

    def _on_session_failed(self, event: dict, _agent) -> None:
        self.health.append({"kind": "session_failed", "detail": event.get("error") or ""})

    def _on_terminated(self, event: dict, _agent) -> None:
        self.progress["termination"] = event.get("reason")

    # turns ----------------------------------------------------------------

    def _on_turn_start(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        turn = {
            "label": agent,
            "round": _round_of(event),
            "phase": event.get("phase"),
            "verdict": None,
            "comment": "",
            "reason": "",
            "text": "",
            "streaming": True,
            "failed": False,
            "resumed": bool(event.get("resumed")),
            "started_at": event.get("ts"),
        }
        self.open_turns[agent] = turn
        self.activity[agent] = {"state": "thinking", "tool": "", "target": ""}
        self.trails[agent] = []  # a new turn starts a new trail
        if turn["round"]:
            self.rounds.setdefault(turn["round"], []).append(turn)

    def _on_turn_delta(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        kind = event.get("kind")
        if kind == "text":
            turn = self.open_turns.get(agent)
            text = event.get("text")
            if turn is not None and isinstance(text, str):
                merged = turn["text"] + text
                turn["text"] = merged[-LIVE_TEXT_LIMIT:]
            self.activity[agent] = {"state": "writing", "tool": "", "target": ""}
        elif kind == "tool":
            tool = str(event.get("tool") or "")
            target = str(event.get("target") or "")
            self.activity[agent] = {"state": "exploring", "tool": tool, "target": target}
            self._trail(agent, " ".join(x for x in (tool, target) if x))
        elif kind == "status":
            state = str(event.get("text") or "thinking")
            self.activity[agent] = {"state": state, "tool": "", "target": ""}
            self._trail(agent, state)

    def _trail(self, agent: str, line: str) -> None:
        """What this panelist has been doing since its turn began.

        A tool call used to update one caption that the next call overwrote, so a
        panelist reading a repository for three minutes showed a line that flickered and
        nothing else. Codex says almost nothing while it explores — measured at roughly
        one message per four commands, and this version emits no reasoning items at all —
        so this trail is most of what there is to watch.

        Kept per panelist rather than per turn because Phase 1 turns carry round 0 and
        never enter `rounds` at all: hanging it off the turn would have missed the one
        stretch that is longest and quietest.
        """
        if not line:
            return
        trail = self.trails.setdefault(agent, [])
        if trail and trail[-1] == line:
            return  # the same file twice in a row is noise, not progress
        trail.append(line)
        del trail[:-TRAIL_LIMIT]

    def _on_turn_end(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        turn = self.open_turns.pop(agent, None)
        self.activity.pop(agent, None)
        round_no = _round_of(event)
        if turn is None:
            turn = {"label": agent, "round": round_no, "failed": False, "text": ""}
            self.rounds.setdefault(round_no, []).append(turn)
        turn.update(
            streaming=False,
            verdict=event.get("verdict"),
            comment=event.get("comment") or "",
            reason=event.get("reason") or "",
            malformed=bool(event.get("malformed")),
            resumed=bool(event.get("resumed")),
            seconds=event.get("seconds"),
            tokens=event.get("tokens"),
        )
        self._account(agent, event.get("tokens"))

    def _on_turn(self, event: dict, agent) -> None:
        """V1: a completed turn arrived as a single record."""
        if not isinstance(agent, str):
            return
        round_no = _round_of(event)
        self.rounds.setdefault(round_no, []).append(
            {
                "label": agent,
                "round": round_no,
                "verdict": event.get("verdict"),
                "comment": event.get("comment") or "",
                "reason": event.get("reason") or "",
                "text": "",
                "streaming": False,
                "failed": False,
                "malformed": bool(event.get("malformed")),
                "resumed": bool(event.get("resumed")),
                "seconds": event.get("seconds"),
                "tokens": event.get("tokens"),
            }
        )
        self._account(agent, event.get("tokens"))

    def _on_turn_failed(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        round_no = _round_of(event)
        turn = self.open_turns.pop(agent, None)
        self.activity.pop(agent, None)
        if turn is None:
            turn = {"label": agent, "round": round_no, "text": ""}
            self.rounds.setdefault(round_no, []).append(turn)
        turn.update(
            streaming=False, failed=True, verdict=None,
            note=event.get("error") or "", comment="", reason="",
        )
        self.health.append(
            {
                "kind": "turn_failed",
                "agent": agent,
                "round": round_no,
                "detail": event.get("error") or "",
            }
        )

    def _on_chair_message(self, event: dict, _agent) -> None:
        round_no = _round_of(event)
        self.rounds.setdefault(round_no, []).append(
            {
                "label": "Chair",
                "round": round_no,
                "chair": True,
                "by": event.get("by") or "user",
                "comment": event.get("text") or "",
                "text": "",
                "verdict": None,
                "reason": "",
                "streaming": False,
                "failed": False,
            }
        )

    # everything else ------------------------------------------------------

    def _on_plan_received(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        # Phase 1 has no `turn_end`: the plan *is* the end of that turn. Without this
        # the panelist stays "speaking" for the rest of the session.
        self.open_turns.pop(agent, None)
        self.activity.pop(agent, None)
        self.plans[agent] = {
            "label": agent,
            "chars": event.get("chars"),
            "seconds": event.get("seconds"),
            "tokens": event.get("tokens"),
            "session": event.get("session"),
        }
        self._account(agent, event.get("tokens"))

    def _on_panelist_dropped(self, event: dict, agent) -> None:
        if not isinstance(agent, str):
            return
        reason = str(event.get("reason") or "")
        self.dropped[agent] = reason
        self.health.append({"kind": "dropped", "agent": agent, "detail": reason})

    def _on_panelist_restored(self, event: dict, agent) -> None:
        """The orchestrator really put this panelist back; the panel must agree.

        Without this the restore button worked — the panelist spoke again on the next
        round — while every view went on showing it greyed out as dropped, offering
        only the restore that had already happened.
        """
        if isinstance(agent, str):
            self.dropped.pop(agent, None)
            self.health.append({"kind": "restored", "agent": agent, "detail": ""})

    def _on_control_refused(self, event: dict, agent) -> None:
        """A control the orchestrator declined, so the UI does not show it as applied."""
        self.health.append(
            {
                "kind": "refused",
                "agent": agent if isinstance(agent, str) else None,
                "detail": f"{event.get('action')}: {event.get('reason') or 'refused'}",
            }
        )

    def _on_turn_skipped(self, event: dict, agent) -> None:
        """A skipped turn leaves no turn behind, so this is its only trace."""
        if isinstance(agent, str):
            self.health.append(
                {
                    "kind": "skipped",
                    "agent": agent,
                    "round": event.get("round"),
                    "detail": "sat this round out at your request",
                }
            )

    def _on_session_fallback(self, event: dict, agent) -> None:
        self.health.append(
            {
                "kind": "fallback",
                "agent": agent,
                "round": event.get("round"),
                "detail": event.get("error") or "",
            }
        )

    def _on_compacted(self, event: dict, agent) -> None:
        self.compactions.append(
            {"through_round": event.get("through_round"), "agent": agent}
        )

    def _on_compaction_failed(self, event: dict, _agent) -> None:
        self.health.append(
            {"kind": "compaction_failed", "detail": event.get("error") or ""}
        )

    def _on_early_ready_ignored(self, event: dict, _agent) -> None:
        self.health.append(
            {
                "kind": "early_ready",
                "round": event.get("round"),
                "detail": "all READY before min_rounds — discussion continued",
            }
        )

    def _on_control(self, event: dict, _agent) -> None:
        self.controls.append(
            {
                "action": event.get("action"),
                "by": event.get("by"),
                "detail": event.get("detail"),
                "ts": event.get("ts"),
            }
        )

    def _on_budget_extended(self, event: dict, _agent) -> None:
        """An accepted `extend` really changed the protocol; the settings must agree.

        Otherwise a session raised to eight rounds keeps reporting the five it was
        convened with, and the one screen that claims to say how this council is
        configured is the one screen that is wrong about it.
        """
        field = event.get("field")
        if isinstance(field, str):
            protocol = dict(self.meta.get("protocol") or {})
            protocol[field] = event.get("value")
            self.meta["protocol"] = protocol

    def _account(self, agent: str, tokens: object) -> None:
        if isinstance(tokens, int):
            self.tokens_by_agent[agent] = self.tokens_by_agent.get(agent, 0) + tokens

    # ---- output ----------------------------------------------------------

    def panel(self) -> list[dict]:
        order = list(self.order)
        for label in self.identities:
            if label not in order:
                order.append(label)
        for turns in self.rounds.values():
            for turn in turns:
                label = turn.get("label")
                if isinstance(label, str) and label != "Chair" and label not in order:
                    order.append(label)

        latest: dict[str, dict] = {}
        for round_no in sorted(self.rounds):
            for turn in self.rounds[round_no]:
                if not turn.get("failed") and not turn.get("chair") and turn.get("verdict"):
                    latest[turn["label"]] = turn

        panel = []
        for label in sorted(set(order)):
            turn = latest.get(label)
            panel.append(
                {
                    "label": label,
                    "name": self.identities.get(label),
                    "model": self.models.get(label),
                    "adapter": self.adapters.get(label),
                    "effort": self.efforts.get(label),
                    "trail": self.trails.get(label, []),
                    "dropped": label in self.dropped,
                    "drop_reason": self.dropped.get(label),
                    "verdict": turn.get("verdict") if turn else None,
                    "reason": turn.get("reason") if turn else "",
                    "tokens": self.tokens_by_agent.get(label, 0),
                    "has_plan": label in self.plans,
                    "plan": self.plans.get(label),
                    "speaking": label in self.open_turns,
                    "activity": self.activity.get(label),
                }
            )
        return panel

    def rounds_list(self) -> list[dict]:
        return [
            {"round": n, "turns": self.rounds[n]} for n in sorted(self.rounds) if n
        ]


def build_state(session_dir: str | Path, status: dict | None = None) -> dict:
    """Everything a client shows, assembled from the session's own files."""
    session_dir = Path(session_dir)
    status = read_json(session_dir / "status.json") if status is None else status
    reducer = Reducer()
    for event in read_events(session_dir):
        reducer.feed(event)
    return compose_state(session_dir, reducer, status)


def compose_state(session_dir: str | Path, reducer: "Reducer", status: dict) -> dict:
    """The client-facing shape, from an already-folded reducer.

    The daemon keeps a reducer fed live and calls this directly; a finished session
    reaches the same function through `build_state`. One composer, so a live view and
    a reload cannot disagree about what a session looks like.
    """
    session_dir = Path(session_dir)
    state = str(status.get("state") or "unknown")
    heartbeat = age_seconds(status.get("updated_at"))
    live = (
        state == "running"
        and pid_alive(status.get("pid"))
        and (heartbeat is None or heartbeat < STALE_AFTER_SECONDS)
    )
    if state == "running" and not live:
        state = "interrupted"

    meta = reducer.meta
    task_path = session_dir / "task.md"
    seed_path = session_dir / "seed.md"
    context_path = session_dir / "context.md"
    digest_path = session_dir / "digest.md"

    return {
        "session": {
            "id": meta.get("id") or session_dir.name,
            "name": session_dir.name,
            "dir": str(session_dir),
            "project_dir": meta.get("project_dir") or str(session_dir.parent.parent),
            "mode": meta.get("mode") or "independent",
            "protocol": meta.get("protocol") or {},
            "timeouts": meta.get("timeouts") or {},
            "started_at": meta.get("started_at"),
            "task": _read_text(task_path),
            "seed": _read_text(seed_path),
            "context": _read_text(context_path),
        },
        "status": {
            "state": state,
            "raw_state": status.get("state"),
            "live": live,
            "paused": bool(status.get("paused")),
            "phase": status.get("phase", reducer.progress.get("phase")),
            "round": status.get("round", reducer.progress.get("round")),
            # Heartbeats land at every turn; status.json only at phase and round
            # changes. Prefer the fresher of the two.
            "elapsed": reducer.progress.get("elapsed", status.get("elapsed")),
            "tokens": reducer.progress.get("tokens", status.get("tokens")),
            "rounds": status.get("rounds", reducer.progress.get("rounds")),
            "termination": status.get("termination", reducer.progress.get("termination")),
            "error": status.get("error"),
            "heartbeat_age": heartbeat,
        },
        "panel": reducer.panel(),
        "rounds": reducer.rounds_list(),
        "health": reducer.health,
        "controls": reducer.controls,
        "compactions": reducer.compactions,
        "has_digest": digest_path.is_file(),
        "digest": _read_text(digest_path),
        "seq": reducer.seq,
    }


def build_agent_thread(session_dir: str | Path, label: str) -> dict:
    """One panelist's own conversation: what we sent it, and what it did with it.

    This is the answer to "what actually happened inside Agent-C" — the prompts, the
    files it opened, its replies, its failures — and it is the first thing to read when
    a panelist behaves strangely.
    """
    session_dir = Path(session_dir)
    entries: list[dict] = []
    identity: str | None = None
    model: str | None = None
    tokens = 0

    for event in read_events(session_dir):
        kind = event.get("event")
        if kind in ("session_created", "session_start"):
            ids = event.get("identities")
            if isinstance(ids, dict):
                identity = ids.get(label)
            for entry in _as_list(event.get("panel")):
                if isinstance(entry, dict) and entry.get("label") == label:
                    model = entry.get("model")
            continue
        if event.get("agent") != label:
            continue

        if isinstance(event.get("tokens"), int):
            tokens += event["tokens"]

        if kind == "prompt":
            entries.append(
                {
                    "role": "sent",
                    "seq": event.get("seq"),
                    "ts": event.get("ts"),
                    "round": event.get("round"),
                    "phase": event.get("phase"),
                    "text": event.get("text") or "",
                }
            )
        elif kind == "turn_delta" and event.get("kind") == "tool":
            entries.append(
                {
                    "role": "tool",
                    "seq": event.get("seq"),
                    "ts": event.get("ts"),
                    "tool": event.get("tool"),
                    "target": event.get("target"),
                }
            )
        elif kind in ("turn_end", "plan_received"):
            entries.append(
                {
                    "role": "reply",
                    "seq": event.get("seq"),
                    "ts": event.get("ts"),
                    "round": event.get("round"),
                    "phase": 1 if kind == "plan_received" else 2,
                    "text": event.get("comment") or _plan_text(session_dir, label),
                    "verdict": event.get("verdict"),
                    "reason": event.get("reason"),
                    "seconds": event.get("seconds"),
                    "tokens": event.get("tokens"),
                    "session": event.get("session"),
                }
            )
        elif kind in ("turn_failed", "panelist_dropped", "session_fallback"):
            entries.append(
                {
                    "role": "problem",
                    "seq": event.get("seq"),
                    "ts": event.get("ts"),
                    "kind": kind,
                    "detail": event.get("error") or event.get("reason") or "",
                }
            )

    return {
        "label": label,
        "name": identity,
        "model": model,
        "tokens": tokens,
        "entries": entries,
    }


def _plan_text(session_dir: Path, label: str) -> str:
    letter = label.rsplit("-", 1)[-1].lower()
    return _read_text(session_dir / "plans" / f"agent-{letter}.md")


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
