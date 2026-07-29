"""Panelist identities and anonymisation.

The debate is between arguments, not brands: every panelist is referred to by an
anonymous letter (Agent-A, Agent-B, ...) in every prompt and in every human-facing
artefact. The letter -> real model mapping is shuffled per session and recorded only
in events.jsonl.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass

from .config import CouncilConfig, PanelistConfig


@dataclass
class Panelist:
    """A panelist bound to an anonymous letter for one session."""

    name: str
    letter: str
    adapter: str
    model: str | None = None
    focus: str | None = None
    variant: str | None = None

    @property
    def label(self) -> str:
        return f"Agent-{self.letter}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.label


def _letters(n: int) -> list[str]:
    if n <= len(string.ascii_uppercase):
        return list(string.ascii_uppercase[:n])
    # Beyond 26 panelists (never in practice): A..Z, AA, AB, ...
    out = list(string.ascii_uppercase)
    i = 0
    while len(out) < n:
        out.append("A" + string.ascii_uppercase[i])
        i += 1
    return out[:n]


def build_panel(
    config: CouncilConfig, seed: int | None = None, anonymize: bool | None = None
) -> list[Panelist]:
    """Assign anonymous letters to the configured panelists.

    The assignment is shuffled so a letter cannot be traced to a model by its
    position in council.yaml. `seed` makes the shuffle deterministic for tests.
    """
    entries: list[PanelistConfig] = config.enabled_panel
    letters = _letters(len(entries))
    use_focus = config.protocol.focus_roles

    if anonymize is None:
        anonymize = config.protocol.anonymize

    order = list(range(len(entries)))
    if anonymize:
        random.Random(seed).shuffle(order)

    panel: list[Panelist] = []
    for letter, idx in zip(letters, order):
        entry = entries[idx]
        panel.append(
            Panelist(
                name=entry.name,
                letter=letter,
                adapter=entry.adapter,
                model=entry.model,
                focus=entry.focus if use_focus else None,
                variant=entry.variant,
            )
        )
    # Speaking order follows the letters (A, B, C, ...), not council.yaml order.
    panel.sort(key=lambda p: p.letter)
    return panel


def identity_map(panel: list[Panelist]) -> dict[str, str]:
    """letter -> real panelist name. For events.jsonl only, never for prompts."""
    return {p.label: p.name for p in panel}
