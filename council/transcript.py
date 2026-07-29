"""The conversation store.

Harnesses are stateless: every call is a fresh process that remembers nothing.
There is therefore no live chat room — *this* is the chat room. Each turn is
appended here, and every panelist prompt is re-assembled from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .envelope import CONTINUE, READY, Envelope
from .panel import Panelist

RAW_ROUNDS_KEPT = 2


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token estimate (~4 chars/token)."""
    return len(text) // 4


@dataclass
class Turn:
    round: int
    label: str
    comment: str
    verdict: str
    reason: str = ""
    malformed: bool = False
    failed: bool = False
    note: str = ""

    @classmethod
    def from_envelope(cls, round_no: int, label: str, envelope: Envelope) -> "Turn":
        return cls(
            round=round_no,
            label=label,
            comment=envelope.comment,
            verdict=envelope.verdict,
            reason=envelope.reason,
            malformed=envelope.malformed,
        )

    @classmethod
    def failure(cls, round_no: int, label: str, note: str) -> "Turn":
        return cls(
            round=round_no,
            label=label,
            comment="",
            verdict=CONTINUE,
            failed=True,
            note=note,
        )

    def render(self) -> str:
        if self.failed:
            return f"### {self.label} — round {self.round}\n\n_(no response: {self.note})_"
        head = f"### {self.label} — round {self.round}\n\n{self.comment.strip()}"
        tail = f"\n\n**Verdict: {self.verdict}**"
        if self.reason:
            tail += f" — {self.reason.strip()}"
        return head + tail


@dataclass
class Transcript:
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    summarised_through_round: int = 0
    plans: dict[str, str] = field(default_factory=dict)

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)

    @property
    def rounds_held(self) -> int:
        return max((t.round for t in self.turns), default=0)

    def turns_in_round(self, round_no: int) -> list[Turn]:
        return [t for t in self.turns if t.round == round_no]

    def last_turn_of(self, label: str) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.label == label and not turn.failed:
                return turn
        return None

    def all_ready(self, round_no: int, expected_labels: list[str]) -> bool:
        """True when every panelist that was due to speak this round said READY.

        A panelist that failed this round has not consented to ending, so a round
        with any failure can never terminate the debate naturally.
        """
        spoken = {t.label: t for t in self.turns_in_round(round_no)}
        if set(spoken) != set(expected_labels):
            return False
        return all(
            not t.failed and t.verdict == READY for t in spoken.values()
        )

    # ---- prompt assembly -------------------------------------------------

    def render_plans(self, reader: Panelist | None = None) -> str:
        if not self.plans:
            return "_(no plans available)_"
        blocks = []
        for label in sorted(self.plans):
            marker = ""
            if reader is not None and label == reader.label:
                marker = "  ← THIS IS YOUR PLAN"
            blocks.append(f"## {label}'s plan{marker}\n\n{self.plans[label].strip()}")
        return "\n\n---\n\n".join(blocks)

    def render_conversation(self) -> str:
        """The conversation as panelists see it: summary (if any) + recent rounds raw."""
        if not self.turns and not self.summary:
            return "_(no discussion yet — this is the first round)_"

        parts: list[str] = []
        if self.summary:
            parts.append(
                f"## Summary of rounds 1-{self.summarised_through_round}\n\n"
                f"{self.summary.strip()}"
            )
        raw = [t for t in self.turns if t.round > self.summarised_through_round]
        if raw:
            parts.append("\n\n".join(t.render() for t in raw))
        return "\n\n---\n\n".join(parts) if parts else "_(no discussion yet)_"

    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render_conversation())

    def needs_compaction(self, threshold: int) -> bool:
        if self.rounds_held <= RAW_ROUNDS_KEPT:
            return False
        return self.estimated_tokens() > threshold

    def rounds_to_compact(self) -> list[int]:
        """Rounds eligible for summarising: everything but the last RAW_ROUNDS_KEPT."""
        cutoff = self.rounds_held - RAW_ROUNDS_KEPT
        return [
            r
            for r in range(self.summarised_through_round + 1, cutoff + 1)
            if self.turns_in_round(r)
        ]

    def text_to_compact(self, rounds: list[int]) -> str:
        chunks = []
        if self.summary:
            chunks.append(
                f"## Existing summary of rounds 1-{self.summarised_through_round}\n\n"
                f"{self.summary.strip()}"
            )
        for r in rounds:
            chunks.extend(t.render() for t in self.turns_in_round(r))
        return "\n\n".join(chunks)

    def apply_compaction(self, summary: str, through_round: int) -> None:
        self.summary = summary.strip()
        self.summarised_through_round = through_round

    # ---- artefacts -------------------------------------------------------

    def render_transcript_md(self, header: str = "") -> str:
        lines = ["# Council transcript", ""]
        if header:
            lines += [header, ""]
        if self.summary:
            lines += [
                f"> Rounds 1-{self.summarised_through_round} were compacted during the "
                "run; their summary is below, later rounds are verbatim.",
                "",
                f"## Compacted summary (rounds 1-{self.summarised_through_round})",
                "",
                self.summary.strip(),
                "",
            ]
        current = None
        for turn in self.turns:
            if turn.round != current:
                current = turn.round
                lines += [f"## Round {current}", ""]
            lines += [turn.render(), ""]
        return "\n".join(lines).rstrip() + "\n"
