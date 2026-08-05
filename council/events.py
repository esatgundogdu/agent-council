"""The session log — one monotonic sequence, two files, live subscribers.

Everything anyone knows about a session is a fold over this log. The daemon's memory,
an HTTP snapshot, an SSE reconnect and a post-hoc read of a finished session all go
through the same records, so there is no second state representation to drift.

Two files, one counter:

* ``events.jsonl`` — the **semantic** log. Small, durable, and the actual contract:
  the digest, the tests and any replay depend only on this.
* ``stream.jsonl`` — the **verbose** log: text and tool deltas while a turn is in
  flight. High volume — and load-bearing: the per-panelist thread view is rebuilt
  from the prompts and deltas recorded here, so pruning it empties that screen.

Both draw from the same ``seq``, so a single `Last-Event-ID` resumes both coherently.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SEMANTIC_FILE = "events.jsonl"
VERBOSE_FILE = "stream.jsonl"

#: Bumped when the meaning of an event changes. Readers accept older sessions.
SCHEMA_VERSION = 3

#: Per-subscriber backlog. A client that cannot keep up is told to resync rather than
#: being allowed to grow the daemon's memory without bound.
QUEUE_LIMIT = 4096


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Subscription:
    """A live feed of one session's events, with a lag signal.

    `lagged` means events were dropped for this subscriber; the client should refetch
    the snapshot rather than trust that it has seen everything.
    """

    def __init__(self, log: "EventLog", from_seq: int) -> None:
        self._log = log
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.from_seq = from_seq
        self.lagged = False
        self.closed = False

    def _offer(self, record: dict) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.lagged = True

    async def get(self, timeout: float | None = None) -> dict | None:
        """Next event, or None if `timeout` elapsed first (used to send SSE keepalives)."""
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.closed = True
        self._log.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class EventLog:
    """Append-only session log with live fan-out.

    Every mutation of a session goes through `emit`, which assigns the sequence
    number, writes the record and hands it to every live subscriber — in that order,
    so a subscriber can never see an event that is not yet on disk.
    """

    def __init__(self, session_dir: str | Path) -> None:
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = _highest_seq(self.dir)
        self._subscribers: set[Subscription] = set()
        self._handles: dict[str, object] = {}
        #: Called synchronously with every record, in order. The daemon folds its
        #: live snapshot here, so a snapshot request never has to re-read the files.
        self._listeners: list = []

    @property
    def seq(self) -> int:
        return self._seq

    # ---- writing ---------------------------------------------------------

    def emit(self, event_kind: str, /, *, verbose: bool = False, **payload) -> dict:
        # Positional-only: a delta's own `kind` field must be able to travel in the
        # payload without colliding with the name of the event being emitted.
        self._seq += 1
        record = {"seq": self._seq, "ts": utcnow(), "event": event_kind, **payload}
        self._append(VERBOSE_FILE if verbose else SEMANTIC_FILE, record)
        for listener in self._listeners:
            listener(record)
        for sub in list(self._subscribers):
            sub._offer(record)
        return record

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def _append(self, filename: str, record: dict) -> None:
        handle = self._handles.get(filename)
        if handle is None:
            handle = (self.dir / filename).open("a", encoding="utf-8")
            self._handles[filename] = handle
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:  # pragma: no cover - nothing useful to do
                pass
        self._handles.clear()

    # ---- reading ---------------------------------------------------------

    def subscribe(self, from_seq: int = 0) -> Subscription:
        sub = Subscription(self, from_seq)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    def replay(self, from_seq: int = 0, verbose: bool = True) -> list[dict]:
        return read_events(self.dir, from_seq=from_seq, verbose=verbose)


def read_events(
    session_dir: str | Path, from_seq: int = 0, verbose: bool = True
) -> list[dict]:
    """Every record of a session after `from_seq`, in sequence order.

    Tolerates a half-written final line: the file is appended to while we read it, so
    a truncated tail is expected rather than an error — it is complete on the next read.
    Records from a pre-sequence session keep their file order.
    """
    session_dir = Path(session_dir)
    names = [SEMANTIC_FILE] + ([VERBOSE_FILE] if verbose else [])
    records: list[dict] = []
    for name in names:
        records.extend(_read_file(session_dir / name))

    # A seq that is not an integer is repaired rather than trusted: sorting a mix of
    # int and None or str raises TypeError, and that took out the whole session — one
    # corrupt byte made it permanently unviewable, as a 500 from every endpoint. A
    # legacy session (no sequence numbers at all) is numbered by position, which is
    # what it was written with.
    def usable(record: dict) -> bool:
        seq = record.get("seq")
        return isinstance(seq, int) and not isinstance(seq, bool) and seq > 0

    if all(usable(r) for r in records):
        records.sort(key=lambda r: r["seq"])
    else:
        # A log with even one unusable sequence number is renumbered whole, by file
        # order, which is chronological because the log is append-only. Repairing just
        # the bad records instead produced duplicate sequence numbers, and a duplicate
        # is worse than a renumbering: an SSE client resuming from `last` skips
        # everything `<= last`, so the second record at that number is never delivered.
        # A legacy session — no sequence numbers at all — lands here too, as before.
        for i, record in enumerate(records, start=1):
            record["seq"] = i
    return [r for r in records if r["seq"] > from_seq]


def _read_file(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # truncated tail, complete on the next read
        if isinstance(record, dict):
            yield record


def _highest_seq(session_dir: Path) -> int:
    """Resume the counter of an existing session, so ids stay unique across restarts."""
    highest = 0
    for name in (SEMANTIC_FILE, VERBOSE_FILE):
        for record in _read_file(session_dir / name):
            seq = record.get("seq")
            if isinstance(seq, int) and seq > highest:
                highest = seq
    return highest
