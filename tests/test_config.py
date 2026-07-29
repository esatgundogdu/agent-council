import pytest

from council.config import ConfigError, load_config, parse_config

BASE = {
    "panel": [
        {"name": "a", "adapter": "mock"},
        {"name": "b", "adapter": "mock"},
    ]
}


def test_defaults_applied():
    cfg = parse_config(dict(BASE))
    assert cfg.protocol.min_rounds == 2
    assert cfg.protocol.max_rounds == 5
    assert cfg.timeouts.per_call == 300
    assert cfg.on_failure == "skip_with_note"


def test_disabled_panelists_are_excluded():
    cfg = parse_config(
        {
            "panel": [
                {"name": "a", "adapter": "mock"},
                {"name": "b", "adapter": "mock"},
                {"name": "c", "adapter": "claude_cli", "enabled": False},
            ]
        }
    )
    assert [p.name for p in cfg.panel] == ["a", "b"]


def test_panel_below_two_rejected():
    with pytest.raises(ConfigError, match="at least 2"):
        parse_config({"panel": [{"name": "a", "adapter": "mock"}]})


def test_unknown_adapter_rejected():
    with pytest.raises(ConfigError, match="unknown adapter"):
        parse_config(
            {"panel": [{"name": "a", "adapter": "gpt5"}, {"name": "b", "adapter": "mock"}]}
        )


def test_duplicate_names_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(
            {"panel": [{"name": "a", "adapter": "mock"}, {"name": "a", "adapter": "mock"}]}
        )


def test_max_rounds_below_min_rejected():
    with pytest.raises(ConfigError, match="must be >="):
        parse_config({**BASE, "protocol": {"min_rounds": 3, "max_rounds": 2}})


def test_nonpositive_budget_rejected():
    with pytest.raises(ConfigError, match="must be > 0"):
        parse_config({**BASE, "protocol": {"token_budget": 0}})


def test_unknown_protocol_key_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({**BASE, "protocol": {"max_round": 3}})


def test_compaction_panelist_must_be_on_the_panel():
    with pytest.raises(ConfigError, match="compaction_panelist"):
        parse_config({**BASE, "protocol": {"compaction_panelist": "nobody"}})


def test_bad_on_failure_rejected():
    with pytest.raises(ConfigError, match="on_failure"):
        parse_config({**BASE, "on_failure": "explode"})


def test_shipped_council_yaml_is_valid():
    from council import DEFAULT_CONFIG_PATH

    cfg = load_config(DEFAULT_CONFIG_PATH)
    names = [p.name for p in cfg.panel]
    assert len(cfg.panel) >= 2
    assert "claude" in names  # Claude takes part in the discussion by default
    # Compaction drives what the panel remembers, so it must not fall to a cheap model.
    compactor = next(p for p in cfg.panel if p.name == cfg.protocol.compaction_panelist)
    assert compactor.adapter in {"codex_cli", "claude_cli"}
    # The transcript must be summarised before it outgrows a panelist's context.
    assert cfg.protocol.compaction_threshold < cfg.protocol.token_budget


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/council.yaml")
