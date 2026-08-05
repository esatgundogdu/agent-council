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
    """A verdict field, read strictly in one direction and loosely in the other.

    CONTINUE means "keep arguing" and is the safe answer, so anything containing it
    counts. READY ends the council, so it must be the whole field and nothing else:
    a substring test read `"NOT READY"` as consent — the exact opposite of what the
    panelist wrote — and did not even mark the turn malformed, so the event log and
    the digest both recorded a blocked panelist as having agreed.
    """
    if not isinstance(value, str):
        return None
    # `.`, `!` and markdown emphasis are noise around a verdict; `?` is not — it turns
    # a statement into a question, and a question is not consent.
    token = value.strip().strip(".!*_ ").upper()
    if CONTINUE in token:
        return CONTINUE
    if token == READY:
        return READY
    # Mentions READY but is not READY — "NOT READY", "READY?", "READY once fixed".
    # Not a verdict this can honestly read, so it degrades to the malformed path.
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
    return Envelope(
        comment=_unescape_newlines(comment).strip(),
        verdict=verdict,
        reason=_unescape_newlines(reason).strip(),
    )


def _unescape_newlines(text: str) -> str:
    r"""Undo a newline the model escaped twice.

    Some models write `\\n` inside the envelope where they mean a line break: the JSON
    is valid and parses cleanly, and the field then carries the two characters
    backslash-n, which reach the digest as literal `\n` in the middle of a sentence.
    Observed consistently from one real model (gpt-5.6-luna, every turn of a session)
    and never from the other, so it is a habit of particular models rather than a bug
    on either side worth failing over.

    Only when the field has no real line break: a field that is already laid out in
    lines is being read literally, and a `\n` inside it is prose about an escape
    sequence rather than a mistake. That leaves one ambiguous case — a genuinely
    single-line comment that means to talk about `\n` — which loses out, because it is
    far rarer than the mangling it costs to keep.
    """
    if "\n" in text:
        return text
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


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


#: A whole line that says nothing but the verdict — optionally labelled, optionally
#: emphasised, optionally punctuated. The trailing `\s*$` is the load-bearing part.
_PROSE_VERDICT = re.compile(
    r"^[\s>*_#-]*(?:verdict\s*[:=]\s*)?[\s*_]*(READY|CONTINUE)[\s.!*_]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _verdict_from_prose(text: str) -> str | None:
    """Last-ditch: a reply that states its verdict in prose rather than JSON.

    The line must consist of the verdict and nothing else. Without the end anchor
    this matched any line *beginning* with the word, so a panelist writing

        Ready or not, this will lose customer data. We must keep discussing.

    was recorded as READY — and because the scan took the last match, a polite
    "Ready to discuss further next round." at the foot of a list of blockers ended
    the council. That is precisely the invention the module docstring forbids, and
    it fired most often on the commonest model error: a trailing comma, which
    invalidates an explicit CONTINUE envelope and drops the reply onto this path.
    """
    matches = _PROSE_VERDICT.findall(text)
    if not matches:
        return None
    verdicts = {m.upper() for m in matches}
    # A reply that says both is not stating a verdict; it is discussing them.
    if len(verdicts) != 1:
        return CONTINUE
    return verdicts.pop()
