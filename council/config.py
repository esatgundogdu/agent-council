"""Configuration loading and validation (council.yaml -> dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when council.yaml is missing, malformed, or semantically invalid."""


KNOWN_ADAPTERS = {"codex_cli", "opencode_cli", "claude_cli", "mock"}

#: How hard a panelist is told to think. codex and the claude CLI turn out to accept
#: exactly the same five words — verified against both installed binaries, which answer
#: 400 for anything else (`minimal` included) rather than quietly falling back — so this
#: is one vocabulary rather than a translation table.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: Harnesses that can be told. `opencode` has no such control, and accepting the setting
#: for it would mean writing a number in a config file that changes nothing. `mock` is
#: here so a scripted rehearsal exercises the same path a real panel does.
EFFORT_ADAPTERS = {"codex_cli", "claude_cli", "mock"}


@dataclass
class PanelistConfig:
    name: str
    adapter: str
    model: str | None = None
    variant: str | None = None
    #: `low`…`max`, or None for whatever the harness defaults to. Worth setting per
    #: panelist rather than per council: a panel where one member thinks hard and
    #: another answers cheaply is a different instrument from one where all four do
    #: the same thing.
    effort: str | None = None
    #: Where this panelist's harness executable actually is, when a bare name on PATH
    #: will not find it. The ChatGPT desktop app, for one, installs codex under a
    #: content-hashed directory that is on nobody's PATH; naming the file here is
    #: better than a PATH entry, which has to be set before the daemon starts and is
    #: silently wrong for whoever forgets. Omit it and the adapter's usual name is
    #: looked up on PATH as before.
    binary: str | None = None


@dataclass
class ProtocolConfig:
    min_rounds: int = 2
    max_rounds: int = 5
    token_budget: int = 150_000
    wall_clock_budget: int = 1800
    compaction_threshold: int = 12_000
    anonymize: bool = True
    compaction_panelist: str | None = None
    #: Continue each panelist's own harness conversation across rounds, so it keeps
    #: the repo context it built while writing its plan.
    session_continuity: bool = True


@dataclass
class TimeoutConfig:
    per_call: int = 300
    per_call_phase1: int = 600


@dataclass
class CouncilConfig:
    panel: list[PanelistConfig]
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    on_failure: str = "skip_with_note"
    #: Keep every harness process's raw output in `<session>/calls/`. On because the
    #: calls worth reading are the ones that went wrong, and a setting you have to turn
    #: on beforehand is off precisely when it is needed. A few MB per session, deleted
    #: with the session.
    capture_console: bool = True


def check_effort(value: object, adapter: str, who: str) -> None:
    """Refuse an effort the harness would not honour, and say which it is.

    Both failures are silent otherwise: an unknown level reaches the provider and comes
    back a 400 after the session has started, and an effort on a harness with no such
    control is a line in a config file that does nothing at all. Raises `ConfigError`,
    which every caller already turns into a message rather than a traceback.
    """
    if value is None:
        return
    if not isinstance(value, str) or value.lower() not in EFFORT_LEVELS:
        raise ConfigError(
            f"{who}: effort must be one of {', '.join(EFFORT_LEVELS)}, got {value!r}"
        )
    if adapter not in EFFORT_ADAPTERS:
        raise ConfigError(
            f"{who}: the '{adapter}' harness has no reasoning-effort control, so "
            "'effort' would do nothing. Remove it."
        )


def _subdict(raw: Any, key: str) -> dict:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping, got {type(value).__name__}")
    return value


def _build(cls, data: dict, section: str):
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) in '{section}': {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )
    return cls(**data)


def load_config(path: str | Path) -> CouncilConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return parse_config(raw)


#: Everything council.yaml may say at the top level.
SECTIONS = {"panel", "protocol", "timeouts", "on_failure", "capture_console"}


def parse_config(raw: dict) -> CouncilConfig:
    # A misspelled section used to be ignored in silence, so every setting inside it
    # did nothing — the exact failure the strict check on nested keys exists to catch,
    # one level up where it matters more.
    unknown = set(raw) - SECTIONS
    if unknown:
        raise ConfigError(
            f"unknown top-level key(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(SECTIONS))}"
        )

    panel_raw = raw.get("panel")
    if not isinstance(panel_raw, list) or not panel_raw:
        raise ConfigError("'panel' must be a non-empty list of panelists")

    panel: list[PanelistConfig] = []
    for i, entry in enumerate(panel_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"panel[{i}] must be a mapping")
        if entry.get("enabled") is False:
            continue
        entry = {k: v for k, v in entry.items() if k != "enabled"}
        if not entry.get("name"):
            raise ConfigError(f"panel[{i}] is missing 'name'")
        if not entry.get("adapter"):
            raise ConfigError(f"panel entry '{entry['name']}' is missing 'adapter'")
        if entry["adapter"] not in KNOWN_ADAPTERS:
            raise ConfigError(
                f"panel entry '{entry['name']}' has unknown adapter "
                f"'{entry['adapter']}'. Known: {', '.join(sorted(KNOWN_ADAPTERS))}"
            )
        check_effort(entry.get("effort"), entry["adapter"], f"panel entry '{entry['name']}'")
        panel.append(_build(PanelistConfig, entry, f"panel[{i}]"))

    if len(panel) < 2:
        raise ConfigError(
            f"a council needs at least 2 enabled panelists, found {len(panel)}"
        )
    names = [p.name for p in panel]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate panelist name(s): {', '.join(sorted(dupes))}")

    protocol = _build(ProtocolConfig, _subdict(raw, "protocol"), "protocol")
    timeouts = _build(TimeoutConfig, _subdict(raw, "timeouts"), "timeouts")

    if protocol.min_rounds < 1:
        raise ConfigError("protocol.min_rounds must be >= 1")
    if protocol.max_rounds < protocol.min_rounds:
        raise ConfigError(
            f"protocol.max_rounds ({protocol.max_rounds}) must be >= "
            f"min_rounds ({protocol.min_rounds})"
        )
    for label, value in (
        ("protocol.token_budget", protocol.token_budget),
        ("protocol.wall_clock_budget", protocol.wall_clock_budget),
        ("protocol.compaction_threshold", protocol.compaction_threshold),
        ("timeouts.per_call", timeouts.per_call),
        ("timeouts.per_call_phase1", timeouts.per_call_phase1),
    ):
        if value <= 0:
            raise ConfigError(f"{label} must be > 0, got {value}")

    if protocol.compaction_panelist and protocol.compaction_panelist not in names:
        raise ConfigError(
            f"protocol.compaction_panelist '{protocol.compaction_panelist}' "
            f"is not an enabled panelist ({', '.join(names)})"
        )

    on_failure = raw.get("on_failure", "skip_with_note")
    if on_failure not in {"skip_with_note", "abort"}:
        raise ConfigError(
            f"on_failure must be 'skip_with_note' or 'abort', got '{on_failure}'"
        )

    capture_console = raw.get("capture_console", True)
    if not isinstance(capture_console, bool):
        raise ConfigError(
            f"capture_console must be true or false, got {capture_console!r}"
        )

    return CouncilConfig(
        panel=panel,
        protocol=protocol,
        timeouts=timeouts,
        on_failure=on_failure,
        capture_console=capture_console,
    )
