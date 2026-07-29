"""Parsing of the Phase 2 message envelope.

Panelists are asked to end their turn with:

    {"comment": "...", "verdict": "CONTINUE|READY", "reason": "..."}

Models being models, the envelope arrives fenced, bare, surrounded by prose, with
odd key casing, or not at all. Parsing therefore degrades gracefully and *never*
invents a READY: an unparseable reply is taken at face value as a CONTINUE comment,
which errs on the side of more discussion rather than premature termination.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

CONTINUE = "CONTINUE"
READY = "READY"

_FENCED = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


@dataclass
class Envelope:
    comment: str
    verdict: str
    reason: str = ""
    malformed: bool = False

    @property
    def is_ready(self) -> bool:
        return self.verdict == READY


def _balanced_objects(text: str) -> list[str]:
    """Return every balanced {...} span in `text`, outermost only, in order."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start : i + 1])
                    start = -1
    return spans


def _normalise_verdict(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().strip(".!").upper()
    if READY in token and CONTINUE not in token:
        return READY
    if CONTINUE in token:
        return CONTINUE
    return None


def _from_object(obj: dict, fallback_text: str) -> Envelope | None:
    lowered = {str(k).strip().lower(): v for k, v in obj.items()}
    verdict = _normalise_verdict(lowered.get("verdict"))
    if verdict is None:
        return None
    comment = lowered.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        # Envelope carried a verdict but no usable comment: keep the full reply so
        # no reasoning is lost from the transcript.
        comment = fallback_text.strip()
    reason = lowered.get("reason")
    if not isinstance(reason, str):
        reason = ""
    return Envelope(comment=comment.strip(), verdict=verdict, reason=reason.strip())


def parse_envelope(text: str) -> Envelope:
    """Extract the envelope from a panelist reply, degrading to CONTINUE."""
    if not text or not text.strip():
        return Envelope(comment="", verdict=CONTINUE, malformed=True)

    candidates: list[str] = []
    candidates.extend(m.group(1) for m in _FENCED.finditer(text))
    candidates.extend(_balanced_objects(text))

    # Later candidates win: the envelope belongs at the end of the reply.
    for blob in reversed(candidates):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        envelope = _from_object(obj, text)
        if envelope is not None:
            return envelope

    # No usable envelope: treat the whole reply as the comment and keep talking.
    verdict = _verdict_from_prose(text)
    return Envelope(
        comment=text.strip(),
        verdict=verdict or CONTINUE,
        malformed=True,
    )


_PROSE_READY = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?(?:verdict\s*[:=]\s*)?(?:\*\*)?\s*(READY|CONTINUE)\b",
    re.IGNORECASE,
)


def _verdict_from_prose(text: str) -> str | None:
    """Last-ditch: a reply that states its verdict in prose rather than JSON.

    Only a verdict on its own line (optionally 'verdict: X') counts, so the words
    'ready' or 'continue' inside an argument cannot end the debate by accident.
    """
    matches = _PROSE_READY.findall(text)
    if not matches:
        return None
    return matches[-1].upper()
