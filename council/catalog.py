"""Turning what you type into a panel.

`--agents "gpt, glm-5.2, kimi k2.6, claude opus 5"` has to become four panelist
definitions. Every model name here comes from the harness itself, never from a table
in this file — a shipped list of model ids is stale the week after it is written.

The three harnesses answer that differently, so this module does too:

* **opencode** enumerates: `opencode models`.
* **codex** enumerates: `codex debug models` renders its catalogue as JSON, with the
  visibility and priority its own picker uses.
* **claude** does not enumerate at all. `--model` takes either a family alias
  (`opus`, `sonnet`, …), which the server resolves to the current model in that family,
  or a full id which is passed straight through. So ids are **derived** — `opus 5`
  becomes `claude-opus-5` by rule — and the CLI remains the judge of whether one
  exists. Nothing to update when a new model ships.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .adapters.base import resolve_binary
from .config import PanelistConfig

CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

#: Aliases `claude --model` documents. Each always means the newest model in that
#: family, so unlike a version table this cannot go stale — and an unknown word is
#: still accepted, because the CLI is the authority on what exists, not this list.
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")


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


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    """Run a CLI and decode it as UTF-8, whatever the console code page says.

    `text=True` alone decodes with the locale's codec — cp1254 on a Turkish Windows,
    cp932 on a Japanese one — and a model catalogue with a typographic quote in a
    description then dies with a UnicodeDecodeError. These tools all emit UTF-8.
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def opencode_models(binary: str = "opencode") -> tuple[str, ...]:
    """Models opencode can currently reach, as 'provider/model' strings."""
    # resolve_binary, or Windows never finds the `opencode.cmd` npm installs and
    # every Ollama Cloud model silently disappears from the catalogue.
    proc = _run([resolve_binary(binary), "models"], timeout=60)
    if proc is None or proc.returncode != 0:
        return ()
    return tuple(
        line.strip()
        for line in proc.stdout.splitlines()
        if "/" in line.strip() and not line.startswith(" ")
    )


@lru_cache(maxsize=1)
def codex_models(binary: str = "codex") -> tuple[dict, ...]:
    """Codex's own model catalogue, as its picker sees it.

    `codex debug models` renders it as JSON, locally and in milliseconds — no network,
    no model call. Entries carry the `visibility` and `priority` codex uses to build
    its own list, so ours can be ordered the same way. An empty result means codex is
    missing or the subcommand has moved, and callers fall back to passing whatever the
    user typed straight to `codex -m`.
    """
    proc = _run([resolve_binary(binary), "debug", "models"], timeout=30)
    if proc is None or proc.returncode != 0:
        return ()
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return ()

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return ()

    listed = []
    for entry in models:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        if entry.get("visibility") == "hide":
            continue  # internal models codex does not offer either
        listed.append(
            {
                "slug": entry["slug"],
                "display_name": entry.get("display_name") or entry["slug"],
                "description": entry.get("description") or "",
                "priority": entry.get("priority") if isinstance(entry.get("priority"), int) else 999,
            }
        )
    listed.sort(key=lambda m: m["priority"])
    # `base_instructions` is a system prompt of several kilobytes per model; it is
    # dropped above rather than carried into an API response nobody reads it from.
    return tuple(listed)


def codex_model_slugs(binary: str = "codex") -> tuple[str, ...]:
    return tuple(m["slug"] for m in codex_models(binary))


def codex_default_model(binary: str = "codex") -> str | None:
    """The model codex uses when none is given, for display purposes.

    `~/.codex/config.toml` wins if it pins one; otherwise it is whatever codex puts
    first in its own catalogue.
    """
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match:
        return match.group(1)
    catalog = codex_models(binary)
    return catalog[0]["slug"] if catalog else None


def _short_name(model: str) -> str:
    """'ollama-cloud/deepseek-v4-pro' -> 'deepseek'."""
    tail = model.split("/")[-1]
    head = re.split(r"[-:.]", tail)[0]
    return head or tail


def canonical(text: str) -> str:
    """Normalise, treating '.' and '-' as the same separator ('opus 4.5' -> 'opus-4-5')."""
    return normalise(text).replace(".", "-")


def _claude_choice(model: str) -> Choice:
    return Choice(
        adapter="claude_cli",
        model=model or None,
        name="claude",
        display=f"claude ({model or 'CLI default'})",
    )


def _resolve_claude(remainder: str) -> Choice:
    """Derive a `--model` value from what the user typed.

    `claude` alone is the CLI default; a bare family word is the alias for the newest
    model in it; a family with a version becomes the full id by rule, so `opus 5` is
    `claude-opus-5` the day it ships without anything here changing. Whether that id
    exists is the CLI's business, and it says so on the panelist's first turn.
    """
    key = canonical(remainder)
    if not key:
        return _claude_choice("")
    if key in CLAUDE_FAMILIES:
        return _claude_choice(key)  # alias: always the current model in that family

    family = key.split("-", 1)[0]
    if not family.isalpha():
        raise CatalogError(
            f"'{remainder.strip()}' does not look like a Claude model. Use a family "
            f"({', '.join(CLAUDE_FAMILIES)}), a family and version like 'claude opus 5', "
            "or a full id like 'claude-opus-5'."
        )
    return _claude_choice(f"claude-{key}")


def _resolve_codex(spec: str, slugs: tuple[str, ...] | None = None) -> Choice:
    key = normalise(spec)
    if key in {"gpt", "codex", "chatgpt"}:
        default = codex_default_model()
        return Choice(
            adapter="codex_cli",
            model=None,
            name="gpt",
            display=f"gpt ({default or 'codex default'})",
        )

    if slugs is None:
        slugs = codex_model_slugs()
    if not slugs:
        # codex is absent, or `debug models` has moved. Pass it through and let codex
        # reject it — better than refusing a model that may well be valid.
        return Choice("codex_cli", key, "gpt", f"gpt ({key})")

    exact = [s for s in slugs if normalise(s) == key]
    prefix = [s for s in slugs if normalise(s).startswith(key)]
    for bucket in (exact, prefix):
        if len(bucket) == 1:
            return Choice("codex_cli", bucket[0], "gpt", f"gpt ({bucket[0]})")
        if len(bucket) > 1:
            raise CatalogError(
                f"'{spec.strip()}' matches several codex models: "
                f"{', '.join(sorted(bucket))}. Be more specific."
            )
    raise CatalogError(
        f"codex does not offer '{spec.strip()}'. It has: {', '.join(slugs)}."
    )


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


def resolve_spec(
    spec: str,
    models: tuple[str, ...] | None = None,
    codex_slugs: tuple[str, ...] | None = None,
) -> Choice:
    """Resolve one agent spec, e.g. 'claude opus 5' or 'kimi k2.6'.

    `models` and `codex_slugs` override the live catalogues, which is what makes this
    testable: otherwise the answer would depend on which CLIs happen to be installed.
    """
    raw = spec.strip()
    if not raw:
        raise CatalogError("empty agent spec")
    key = normalise(raw)

    # 'claude-opus-5' is already a full model id and goes straight to the CLI;
    # 'claude opus 5' is the friendly spelling of the same thing.
    if raw.lower().startswith("claude-"):
        return _claude_choice(key)
    if key == "claude" or key.startswith("claude-"):
        return _resolve_claude(raw[len("claude"):])
    catalog = opencode_models() if models is None else models

    # A bare 'gpt'/'codex' always means the ChatGPT harness. Otherwise a name that
    # really exists in the opencode catalog wins over the gpt- prefix, so Ollama's
    # 'gpt-oss:20b' is not mistaken for a Codex model it cannot run.
    if key in {"gpt", "codex", "chatgpt"}:
        return _resolve_codex(raw, codex_slugs)
    if any(normalise(m) == key or normalise(m.split("/")[-1]) == key for m in catalog):
        return _resolve_opencode(raw, catalog)
    if key.startswith(("gpt", "codex", "chatgpt")):
        return _resolve_codex(raw, codex_slugs)
    return _resolve_opencode(raw, catalog)


def resolve_agents(
    specs: list[str],
    models: tuple[str, ...] | None = None,
    codex_slugs: tuple[str, ...] | None = None,
) -> list[PanelistConfig]:
    """Resolve a full `--agents` list into panelists with unique names."""
    choices = [resolve_spec(s, models, codex_slugs) for s in specs if s.strip()]
    if len(choices) < 2:
        raise CatalogError(
            f"a council needs at least 2 panelists, got {len(choices)}. "
            "Separate agents with commas, e.g. --agents 'gpt, kimi k2.6, claude opus'"
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


def harness_status(binaries: dict[str, str] | None = None) -> list[dict]:
    """What the three harnesses look like from here, for the "new council" form.

    Presence only — whether the binary resolves. Authentication is not probed: proving
    `codex` can reach a model means paying for a model call, and a form that costs
    money to render is a bad form. A panelist whose CLI is installed but not logged in
    fails on its first turn and is dropped with the CLI's own error.

    `binaries` maps an adapter to the executable council.yaml says to use for it, so a
    harness installed somewhere PATH cannot see is reported as present — and its real
    model catalogue is read — rather than as missing while councils using it run fine.
    """
    binaries = binaries or {}
    codex = binaries.get("codex_cli", "codex")
    claude = binaries.get("claude_cli", "claude")
    opencode = binaries.get("opencode_cli", "opencode")

    codex_present = _present(codex)
    return [
        {
            "adapter": "codex_cli",
            "label": "ChatGPT — codex",
            "binary": codex,
            "available": codex_present,
            "default_model": codex_default_model(codex) if codex_present else None,
            # Real, from `codex debug models`, newest first.
            "models": [
                {"id": m["slug"], "label": m["display_name"], "note": m["description"]}
                for m in (codex_models(codex) if codex_present else ())
            ],
            "enumerable": True,
        },
        {
            "adapter": "claude_cli",
            "label": "Claude — claude",
            "binary": claude,
            "available": _present(claude),
            "default_model": None,
            # The claude CLI cannot list its models, so these are the family aliases it
            # documents — each meaning "the newest one" — and anything else typed here
            # is passed through for the CLI to judge.
            "models": [
                {
                    "id": family,
                    "label": family,
                    "note": f"the current {family.capitalize()} model",
                }
                for family in CLAUDE_FAMILIES
            ],
            "enumerable": False,
        },
        {
            "adapter": "opencode_cli",
            "label": "Via opencode",
            "binary": opencode,
            "available": _present(opencode),
            "default_model": None,
            "models": [
                {"id": m, "label": m.split("/")[-1], "note": m.split("/")[0]}
                for m in opencode_models(opencode)
            ],
            "enumerable": True,
        },
    ]


def configured_binaries(panel: list[PanelistConfig]) -> dict[str, str]:
    """adapter -> the executable this panel says to use for it.

    A panel could in principle name two different codex builds; the first wins, and
    nothing here has to care, because this only decides which one is asked for its
    model list. Each panelist still runs the binary it names.
    """
    out: dict[str, str] = {}
    for entry in panel:
        if entry.binary and entry.adapter not in out:
            out[entry.adapter] = entry.binary
    return out


def _present(binary: str) -> bool:
    """Whether a harness can be started, by PATH lookup or as a named file.

    `resolve_binary` hands an unresolvable name back unchanged, so a bare name that is
    not on PATH comes back as itself — which is not a file, and so reads as absent.
    """
    resolved = resolve_binary(binary)
    return os.path.isfile(resolved) or shutil.which(resolved) is not None


def describe_catalog(binaries: dict[str, str] | None = None) -> str:
    """The listing shown by `council models`.

    Takes the same binary overrides as `harness_status`, so this agrees with what a
    council would actually run rather than with what happens to be on PATH.
    """
    binaries = binaries or {}
    codex = binaries.get("codex_cli", "codex")
    lines = ["Agents you can pass to --agents (comma-separated).", ""]

    default = codex_default_model(codex)
    lines += ["ChatGPT — via codex, your ChatGPT subscription"]
    lines.append(
        f"  {'gpt':<23} codex's own default" + (f" ({default})" if default else "")
    )
    codex_catalog = codex_models(codex)
    if codex_catalog:
        for model in codex_catalog:
            lines.append(f"  {model['slug']:<23} {model['display_name']}")
    else:
        lines.append(
            f"  {'gpt-5.6, …':<23} (codex not found; any id is passed to `codex -m`)"
        )

    lines += [
        "",
        "Claude — via claude CLI, shares your Claude Code quota",
        f"  {'claude':<23} the CLI's own default",
    ]
    for family in CLAUDE_FAMILIES:
        lines.append(f"  {'claude ' + family:<23} the current {family.capitalize()}")
    lines.append(
        f"  {'claude opus 5':<23} a pinned version -> claude-opus-5"
    )

    models = opencode_models(binaries.get("opencode_cli", "opencode"))
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
        "  council start --task task.md --agents 'gpt, glm-5.2, kimi k2.6, claude opus'",
    ]
    return "\n".join(lines)
