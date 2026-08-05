"""How hard each panelist is told to think.

Both harnesses have the control and neither adapter used it, so the panel's reasoning
effort was whatever `~/.codex/config.toml` happened to say — global, invisible from this
repository, and applying to half the panel only. These tests pin the flag to the argv
that actually reaches the harness, because that is the only place the setting is real.
"""

import pytest

from council.adapters.claude_cli import ClaudeAdapter
from council.adapters.codex_cli import CodexAdapter
from council.catalog import CatalogError, parse_agents_argument, resolve_agents, split_effort
from council.config import ConfigError, parse_config
from council.panel import build_panel

CODEX_SLUGS = ("gpt-5.6-sol", "gpt-5.5")


def panel_yaml(**overrides):
    entry = {"name": "gpt", "adapter": "codex_cli", **overrides}
    return {"panel": [entry, {"name": "claude", "adapter": "claude_cli"}]}


# ---- what reaches the harness ------------------------------------------


def test_codex_passes_effort_as_a_config_override(monkeypatch):
    """codex has no --effort; it takes the same setting the config file uses."""
    argv = capture_argv(monkeypatch, CodexAdapter(effort="max"))
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=max"


def test_claude_passes_effort_as_a_flag(monkeypatch):
    argv = capture_argv(monkeypatch, ClaudeAdapter(effort="xhigh"))
    assert argv[argv.index("--effort") + 1] == "xhigh"


@pytest.mark.parametrize("adapter", [CodexAdapter(), ClaudeAdapter()])
def test_no_effort_means_no_flag(monkeypatch, adapter):
    """The harness keeps its own default, rather than being pinned to one of ours."""
    argv = capture_argv(monkeypatch, adapter)
    assert "--effort" not in argv
    assert not any(str(a).startswith("model_reasoning_effort") for a in argv)


def capture_argv(monkeypatch, adapter):
    """Run one `ask` against a stubbed subprocess and return the command line."""
    import asyncio

    from council.adapters import base

    seen: dict = {}

    async def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return base.Reply(ok=True, text="{}")

    monkeypatch.setattr(base, "run_process", fake_run)
    monkeypatch.setattr("council.adapters.codex_cli.run_process", fake_run, raising=False)
    monkeypatch.setattr("council.adapters.claude_cli.run_process", fake_run, raising=False)
    asyncio.run(adapter.ask("hi", cwd=".", timeout=5))
    return seen["argv"]


# ---- config ------------------------------------------------------------


def test_effort_travels_from_the_config_file_to_the_panelist():
    config = parse_config(panel_yaml(effort="high"))
    assert config.panel[0].effort == "high"
    panel = build_panel(config, seed=0, anonymize=False)
    assert next(p for p in panel if p.name == "gpt").effort == "high"


def test_an_unknown_level_is_refused_before_anything_is_spent():
    with pytest.raises(ConfigError, match="effort must be one of"):
        parse_config(panel_yaml(effort="ludicrous"))
    # `minimal` reads like it ought to work; both harnesses answer 400 for it.
    with pytest.raises(ConfigError, match="effort must be one of"):
        parse_config(panel_yaml(effort="minimal"))


def test_effort_on_a_harness_that_cannot_do_it_is_refused_not_ignored():
    """Silently accepting it writes a line in council.yaml that changes nothing."""
    raw = {
        "panel": [
            {"name": "kimi", "adapter": "opencode_cli", "effort": "high"},
            {"name": "claude", "adapter": "claude_cli"},
        ]
    }
    with pytest.raises(ConfigError, match="no reasoning-effort control"):
        parse_config(raw)


# ---- the --agents spelling ---------------------------------------------


def test_an_agent_spec_carries_its_own_effort():
    assert split_effort("gpt@max") == ("gpt", "max")
    assert split_effort("claude opus @ high".replace(" @ ", "@")) == ("claude opus", "high")
    assert split_effort("gpt") == ("gpt", None)


def test_a_colon_would_have_broken_on_a_real_model_id():
    """opencode lists `gpt-oss:20b`; `@` is chosen so the separator cannot appear inside."""
    assert split_effort("gpt-oss:20b") == ("gpt-oss:20b", None)


def test_agents_resolves_per_panelist_effort():
    panel = resolve_agents(
        parse_agents_argument("gpt@max, claude opus@low, gpt-5.5"),
        models=(),
        codex_slugs=CODEX_SLUGS,
    )
    assert [p.effort for p in panel] == ["max", "low", None]


def test_a_bad_level_after_the_at_sign_names_the_real_ones():
    with pytest.raises(CatalogError, match="not an effort level"):
        resolve_agents(
            parse_agents_argument("gpt@turbo, claude opus"),
            models=(),
            codex_slugs=CODEX_SLUGS,
        )
