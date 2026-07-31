"""Turning what you type into a panel.

`--agents "gpt, glm-5.2, kimi k2.6, claude opus 4.8"` has to become four panelist
definitions. Ollama Cloud models are discovered live from `opencode models`, since
that list changes often; Claude and Codex models are matched against small curated
tables because neither CLI can enumerate its own models.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .adapters.base import resolve_binary
from .config import PanelistConfig

CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

# `claude --model` takes an alias for the current model in a family, or a full id.
# Aliases are preferred where the user did not pin a version: they keep working.
CLAUDE_MODELS: dict[str, str] = {
    "": "",  # bare "claude": whatever the CLI defaults to
    "opus": "opus",
    "opus-4.8": "claude-opus-4-8",
    "sonnet": "sonnet",
    "sonnet-5": "claude-sonnet-5",
    "haiku": "haiku",
    "haiku-4.5": "claude-haiku-4-5-20251001",
    "fable": "fable",
    "fable-5": "claude-fable-5",
}

CLAUDE_SUGGESTIONS = ["claude", "claude opus 4.8", "claude sonnet 5", "claude haiku 4.5"]

# The Codex CLI cannot list its models, so anything gpt-shaped is passed through to
# `codex -m` and validated by codex itself. These are only shown as suggestions.
CODEX_SUGGESTIONS = ["gpt", "gpt-5.2", "gpt-5.2-codex", "gpt-5.4", "gpt-5.5"]


class CatalogError(Exception):
    """A model spec could not be resolved to exactly one model."""


@dataclass
class Choice:
    adapter: str
    model: str | None
    name: str
    display: str


def normalise(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


@lru_cache(maxsize=1)
def opencode_models(binary: str = "opencode") -> tuple[str, ...]:
    """Models opencode can currently reach, as 'provider/model' strings."""
    try:
        # resolve_binary, or Windows never finds the `opencode.cmd` npm installs and
        # every Ollama Cloud model silently disappears from the catalogue.
        proc = subprocess.run(
            [resolve_binary(binary), "models"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ()
    if proc.returncode != 0:
        return ()
    return tuple(
        line.strip()
        for line in proc.stdout.splitlines()
        if "/" in line.strip() and not line.startswith(" ")
    )


def codex_default_model() -> str | None:
    """The model codex uses when none is given, for display purposes."""
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _short_name(model: str) -> str:
    """'ollama-cloud/deepseek-v4-pro' -> 'deepseek'."""
    tail = model.split("/")[-1]
    head = re.split(r"[-:.]", tail)[0]
    return head or tail


def canonical(text: str) -> str:
    """Normalise, treating '.' and '-' as the same separator ('opus 4.8' -> 'opus-4-8')."""
    return normalise(text).replace(".", "-")


def _claude_choice(model: str) -> Choice:
    return Choice(
        adapter="claude_cli",
        model=model or None,
        name="claude",
        display=f"claude ({model or 'CLI default'})",
    )


def _resolve_claude(remainder: str) -> Choice:
    key = canonical(remainder)
    lookup = {canonical(k): v for k, v in CLAUDE_MODELS.items()}
    if key in lookup:
        return _claude_choice(lookup[key])
    raise CatalogError(
        f"unknown Claude model '{remainder.strip()}'. Try one of: "
        + ", ".join(CLAUDE_SUGGESTIONS)
    )


def _resolve_codex(spec: str) -> Choice:
    key = normalise(spec)
    if key in {"gpt", "codex", "chatgpt"}:
        default = codex_default_model()
        return Choice(
            adapter="codex_cli",
            model=None,
            name="gpt",
            display=f"gpt ({default or 'codex default'})",
        )
    return Choice("codex_cli", key, "gpt", f"gpt ({key})")


def _resolve_opencode(spec: str, models: tuple[str, ...]) -> Choice:
    key = normalise(spec)
    if not models:
        raise CatalogError(
            f"cannot resolve '{spec}': `opencode models` returned nothing. "
            "Check that opencode is installed and authenticated."
        )

    exact = [m for m in models if normalise(m) == key or normalise(m.split("/")[-1]) == key]
    prefix = [m for m in models if normalise(m.split("/")[-1]).startswith(key)]
    loose = [m for m in models if key in normalise(m)]

    for bucket in (exact, prefix, loose):
        if len(bucket) == 1:
            model = bucket[0]
            return Choice("opencode_cli", model, _short_name(model), model)
        if len(bucket) > 1:
            raise CatalogError(
                f"'{spec}' matches several models: {', '.join(sorted(bucket))}. "
                "Be more specific."
            )

    close = [m for m in models if key.split("-")[0] in normalise(m)]
    hint = f" Did you mean: {', '.join(sorted(close)[:5])}?" if close else ""
    raise CatalogError(f"no model matches '{spec}'.{hint} Run `council models` to list them.")


def resolve_spec(spec: str, models: tuple[str, ...] | None = None) -> Choice:
    """Resolve one agent spec, e.g. 'claude opus 4.8' or 'kimi k2.6'."""
    raw = spec.strip()
    if not raw:
        raise CatalogError("empty agent spec")
    key = normalise(raw)

    # 'claude-opus-4-8' is a full model id and goes straight to the CLI, so a model
    # released after this table was written still works. 'claude opus 4.8' is the
    # friendly form and must resolve against the table.
    if raw.lower().startswith("claude-"):
        return _claude_choice(key)
    if key == "claude" or key.startswith("claude-"):
        return _resolve_claude(raw[len("claude"):])
    catalog = opencode_models() if models is None else models

    # A bare 'gpt'/'codex' always means the ChatGPT harness. Otherwise a name that
    # really exists in the opencode catalog wins over the gpt- prefix, so Ollama's
    # 'gpt-oss:20b' is not mistaken for a Codex model it cannot run.
    if key in {"gpt", "codex", "chatgpt"}:
        return _resolve_codex(raw)
    if any(normalise(m) == key or normalise(m.split("/")[-1]) == key for m in catalog):
        return _resolve_opencode(raw, catalog)
    if key.startswith(("gpt", "codex", "chatgpt")):
        return _resolve_codex(raw)
    return _resolve_opencode(raw, catalog)


def resolve_agents(
    specs: list[str], models: tuple[str, ...] | None = None
) -> list[PanelistConfig]:
    """Resolve a full `--agents` list into panelists with unique names."""
    choices = [resolve_spec(s, models) for s in specs if s.strip()]
    if len(choices) < 2:
        raise CatalogError(
            f"a council needs at least 2 panelists, got {len(choices)}. "
            "Separate agents with commas, e.g. --agents 'gpt, kimi k2.6, claude opus 4.8'"
        )

    seen: dict[str, int] = {}
    panel: list[PanelistConfig] = []
    for choice in choices:
        seen[choice.name] = seen.get(choice.name, 0) + 1
        name = choice.name if seen[choice.name] == 1 else f"{choice.name}-{seen[choice.name]}"
        panel.append(
            PanelistConfig(name=name, adapter=choice.adapter, model=choice.model)
        )
    return panel


def parse_agents_argument(value: str) -> list[str]:
    """Split an --agents value on commas (models never contain one)."""
    return [part.strip() for part in value.split(",") if part.strip()]


def describe_catalog() -> str:
    """The listing shown by `council models`."""
    lines = ["Agents you can pass to --agents (comma-separated).", ""]

    default = codex_default_model()
    lines += [
        "ChatGPT — via codex, your ChatGPT subscription",
        f"  gpt                     current codex default"
        + (f" ({default})" if default else ""),
        "  gpt-5.2, gpt-5.4, ...   any model id, passed straight to `codex -m`",
        "",
        "Claude — via claude CLI, shares your Claude Code quota",
    ]
    for spec in CLAUDE_SUGGESTIONS:
        target = (
            "CLI default" if spec == "claude" else CLAUDE_MODELS[normalise(spec[7:])]
        )
        lines.append(f"  {spec:<23} {target}")

    models = opencode_models()
    if not models:
        lines += ["", "Via opencode", "  (none found — is opencode installed and authenticated?)"]
    else:
        by_provider: dict[str, list[str]] = {}
        for model in models:
            by_provider.setdefault(model.split("/")[0], []).append(model)
        for provider, entries in sorted(by_provider.items()):
            lines += ["", f"Via opencode — {provider}"]
            for model in entries:
                lines.append(f"  {model.split('/')[-1]:<23} {model}")

    lines += [
        "",
        "Example:",
        "  council run --task task.md --agents 'gpt, glm-5.2, kimi k2.6, claude opus 4.8'",
    ]
    return "\n".join(lines)
