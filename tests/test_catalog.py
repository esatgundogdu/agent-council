import pytest

from council.catalog import (
    CatalogError,
    normalise,
    parse_agents_argument,
    resolve_agents,
    resolve_spec,
)

MODELS = (
    "ollama-cloud/deepseek-v4-flash",
    "ollama-cloud/deepseek-v4-pro",
    "ollama-cloud/glm-5.1",
    "ollama-cloud/glm-5.2",
    "ollama-cloud/kimi-k2.5",
    "ollama-cloud/kimi-k2.6",
    "ollama-cloud/qwen3.5:397b",
)


def r(spec):
    return resolve_spec(spec, MODELS)


# ---- chatgpt ------------------------------------------------------------


def test_bare_gpt_uses_the_codex_default():
    c = r("gpt")
    assert (c.adapter, c.model, c.name) == ("codex_cli", None, "gpt")


def test_pinned_gpt_model_is_passed_through():
    c = r("gpt-5.2")
    assert (c.adapter, c.model) == ("codex_cli", "gpt-5.2")


def test_unknown_gpt_model_is_still_accepted():
    # codex validates its own model names; we must not second-guess them.
    assert r("gpt-9-turbo").model == "gpt-9-turbo"


# ---- claude -------------------------------------------------------------


def test_bare_claude_uses_the_cli_default():
    c = r("claude")
    assert (c.adapter, c.model, c.name) == ("claude_cli", None, "claude")


def test_claude_with_spaces_and_dots():
    assert r("claude opus 4.8").model == "claude-opus-4-8"
    assert r("claude sonnet 5").model == "claude-sonnet-5"
    assert r("claude haiku 4.5").model == "claude-haiku-4-5-20251001"


def test_claude_family_alias_is_kept_as_an_alias():
    # 'opus' tracks the latest Opus; pinning it would defeat that.
    assert r("claude opus").model == "opus"


def test_claude_full_model_id():
    assert r("claude-opus-4-8").model == "claude-opus-4-8"


def test_unknown_claude_model_is_rejected_with_suggestions():
    with pytest.raises(CatalogError, match="claude opus 4.8"):
        r("claude wizard 9")


# ---- opencode / ollama cloud -------------------------------------------


def test_exact_model_name():
    c = r("glm-5.2")
    assert (c.adapter, c.model, c.name) == ("opencode_cli", "ollama-cloud/glm-5.2", "glm")


def test_spaces_are_normalised_to_dashes():
    assert r("kimi k2.6").model == "ollama-cloud/kimi-k2.6"


def test_fully_qualified_name():
    assert r("ollama-cloud/glm-5.2").model == "ollama-cloud/glm-5.2"


def test_ambiguous_prefix_is_rejected_rather_than_guessed():
    with pytest.raises(CatalogError, match="matches several"):
        r("kimi")


def test_unknown_model_suggests_alternatives():
    with pytest.raises(CatalogError, match="glm"):
        r("glm-9.9")


def test_panelist_name_drops_the_version():
    assert r("deepseek-v4-pro").name == "deepseek"
    assert r("qwen3.5:397b").name == "qwen3"


# ---- whole panels -------------------------------------------------------


def test_the_example_panel_resolves():
    panel = resolve_agents(
        parse_agents_argument("gpt, glm-5.2, kimi k2.6, claude opus 4.8"), MODELS
    )
    assert [p.name for p in panel] == ["gpt", "glm", "kimi", "claude"]
    assert [p.adapter for p in panel] == [
        "codex_cli", "opencode_cli", "opencode_cli", "claude_cli",
    ]
    assert panel[3].model == "claude-opus-4-8"


def test_duplicate_families_get_distinct_names():
    panel = resolve_agents(["glm-5.1", "glm-5.2"], MODELS)
    assert [p.name for p in panel] == ["glm", "glm-2"]


def test_panel_of_one_is_rejected():
    with pytest.raises(CatalogError, match="at least 2"):
        resolve_agents(["gpt"], MODELS)


def test_whitespace_and_trailing_commas_are_tolerated():
    assert parse_agents_argument(" gpt ,, kimi k2.6 , ") == ["gpt", "kimi k2.6"]


def test_normalise():
    assert normalise("  Claude  Opus_4.8 ") == "claude-opus-4.8"


def test_ollama_gpt_named_model_is_not_hijacked_by_codex():
    """'gpt-oss:20b' is an Ollama model; routing it to codex fails at runtime."""
    models = MODELS + ("ollama-cloud/gpt-oss:20b", "ollama-cloud/gpt-oss:120b")
    c = resolve_spec("gpt-oss:20b", models)
    assert c.adapter == "opencode_cli"
    assert c.model == "ollama-cloud/gpt-oss:20b"


def test_bare_gpt_still_means_codex_even_with_gpt_models_in_the_catalog():
    models = MODELS + ("ollama-cloud/gpt-oss:20b",)
    assert resolve_spec("gpt", models).adapter == "codex_cli"


def test_codex_only_model_still_routes_to_codex():
    models = MODELS + ("ollama-cloud/gpt-oss:20b",)
    c = resolve_spec("gpt-5.6-sol", models)
    assert c.adapter == "codex_cli" and c.model == "gpt-5.6-sol"
