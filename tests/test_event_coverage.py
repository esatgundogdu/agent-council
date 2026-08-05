"""Every event the orchestrator emits must be folded, or deliberately not folded.

This exists because of what driving the finished UI turned up: `panelist_restored` and
`turn_skipped` were emitted correctly, and neither reducer handled either of them. The
restore button worked — the panelist really did rejoin the panel and speak again — while
every view went on showing it dropped, offering the restore that had already happened.

That failure is invisible to every other test here. The orchestrator tests assert the
panel is correct, the state tests assert the fold is correct for the events they feed
it, and neither notices an event that exists on one side of the seam and not the other.
So this asserts the seam itself, for both reducers, and makes ignoring an event a thing
you have to write down.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Events with nothing to fold. Each needs a reason, because "we forgot" and "there is
#: nothing to do" look identical from here.
IGNORED = {
    # Verbose-stream only: the full prompt text, which the semantic log does not carry
    # and the agent-thread view reads out of stream.jsonl directly.
    "prompt",
    # Announces a compaction that `compacted` then reports the result of. Folding the
    # start would show a compaction that may still fail.
    "compaction_start",
}

#: The browser folds only what arrives faster than a debounced refetch can absorb;
#: its `default` branch refetches, so every other event is handled by construction.
BROWSER_FOLDS = {"turn_start", "turn_delta", "heartbeat"}


def emitted_events() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "council").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        names |= set(re.findall(r"_event\(\s*[\"'](\w+)[\"']", source))
        names |= set(re.findall(r"\.emit\(\s*[\"'](\w+)[\"']", source))
    return names


def test_the_orchestrator_emits_events_at_all():
    """A guard on the guard: a rename that broke the regex would silently pass."""
    names = emitted_events()
    assert len(names) > 15
    assert {"turn_end", "plan_received", "panelist_restored"} <= names


def test_every_emitted_event_is_folded_by_the_python_reducer():
    handled = set(
        re.findall(r"def _on_(\w+)", (ROOT / "council" / "state.py").read_text(encoding="utf-8"))
    )
    missing = emitted_events() - handled - IGNORED
    assert not missing, (
        f"emitted but never folded into the session state: {sorted(missing)}. "
        "Add a `_on_<event>` handler in council/state.py, or list it in IGNORED here "
        "with the reason there is nothing to fold."
    )


def test_the_browser_reducer_folds_only_the_live_events_and_refetches_the_rest():
    """The browser must not grow a second copy of the Python reducer again.

    It had one: twenty `case` blocks mirroring `state.py`, already drifted in six
    places, each drift invisible until a reload disagreed with the screen. The rule
    now is narrow — fold what makes a live turn feel live, refetch for everything
    else — and a new `case` is how that rule gets broken.
    """
    source = (ROOT / "web" / "src" / "reduce.ts").read_text(encoding="utf-8")
    handled = set(re.findall(r"case '(\w+)'", source))
    assert handled == BROWSER_FOLDS, (
        f"web/src/reduce.ts folds {sorted(handled)}; expected {sorted(BROWSER_FOLDS)}. "
        "Everything else must fall through to the `default` branch, which refetches "
        "the authoritative snapshot instead of recomputing it in a second language."
    )
    assert "default:" in source and "refetch: true" in source
