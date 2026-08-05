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


#: What `codex debug models` reports here, pinned so the tests do not depend on
#: whether codex happens to be installed on the machine running them.
CODEX_SLUGS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
)


def r(spec):
    return resolve_spec(spec, MODELS, CODEX_SLUGS)


# ---- chatgpt ------------------------------------------------------------


def test_bare_gpt_uses_the_codex_default():
    c = r("gpt")
    assert (c.adapter, c.model, c.name) == ("codex_cli", None, "gpt")


def test_pinned_gpt_model_is_matched_against_the_real_catalogue():
    c = r("gpt-5.5")
    assert (c.adapter, c.model) == ("codex_cli", "gpt-5.5")


def test_gpt_model_can_be_spelled_with_spaces_and_dots():
    assert r("gpt 5.4 mini").model == "gpt-5.4-mini"


def test_an_ambiguous_gpt_prefix_names_the_alternatives():
    with pytest.raises(CatalogError, match="gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra"):
        r("gpt-5.6")


def test_a_model_codex_does_not_offer_is_rejected_with_the_real_list():
    with pytest.raises(CatalogError, match="gpt-5.6-sol"):
        r("gpt-4")


def test_without_a_codex_catalogue_anything_is_passed_through():
    """codex missing, or `debug models` moved: refusing a valid model would be worse."""
    assert resolve_spec("gpt-9-turbo", MODELS, ()).model == "gpt-9-turbo"




# ---- claude -------------------------------------------------------------


def test_bare_claude_uses_the_cli_default():
    c = r("claude")
    assert (c.adapter, c.model, c.name) == ("claude_cli", None, "claude")


def test_a_pinned_claude_version_is_derived_not_looked_up():
    """No table: 'opus 5' becomes 'claude-opus-5' by rule, so a model released
    tomorrow works today. The CLI is what decides whether the id exists."""
    assert r("claude opus 5").model == "claude-opus-5"
    assert r("claude sonnet 5").model == "claude-sonnet-5"
    assert r("claude haiku 4.5").model == "claude-haiku-4-5"


def test_claude_family_alias_is_kept_as_an_alias():
    # 'opus' tracks the latest Opus; pinning it would defeat that.
    assert r("claude opus").model == "opus"


def test_claude_full_model_id():
    assert r("claude-opus-5").model == "claude-opus-5"


def test_an_unknown_claude_family_is_passed_to_the_cli_to_judge():
    # The claude CLI cannot enumerate its models, so this module must not pretend to
    # know which families exist — that is exactly what goes stale.
    assert r("claude quartz 7").model == "claude-quartz-7"


def test_claude_gibberish_is_rejected():
    with pytest.raises(CatalogError, match="does not look like a Claude model"):
        r("claude 9000")


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
        parse_agents_argument("gpt, glm-5.2, kimi k2.6, claude opus 5"), MODELS, CODEX_SLUGS
    )
    assert [p.name for p in panel] == ["gpt", "glm", "kimi", "claude"]
    assert [p.adapter for p in panel] == [
        "codex_cli", "opencode_cli", "opencode_cli", "claude_cli",
    ]
    assert panel[3].model == "claude-opus-5"


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


def test_configured_binaries_are_collected_per_adapter():
    from council.catalog import configured_binaries
    from council.config import PanelistConfig

    panel = [
        PanelistConfig(name="a", adapter="codex_cli", binary="/opt/*/codex"),
        PanelistConfig(name="b", adapter="codex_cli", binary="/other/codex"),
        PanelistConfig(name="c", adapter="claude_cli"),
    ]
    # First wins, and an adapter nobody overrides is simply absent — the caller
    # falls back to the plain name, which is what PATH is for.
    assert configured_binaries(panel) == {"codex_cli": "/opt/*/codex"}


def test_harness_status_believes_a_configured_binary_over_path(tmp_path):
    """A harness the config points at is present, whatever PATH thinks.

    Otherwise the "new council" form warns that codex is missing while councils using
    it run perfectly — the form contradicting the thing it is a form for.
    """
    from council.catalog import harness_status

    installed = tmp_path / "hash1"
    installed.mkdir()
    exe = installed / "codex.exe"
    exe.write_text("", encoding="utf-8")

    named = {h["adapter"]: h for h in harness_status({"codex_cli": str(exe)})}
    assert named["codex_cli"]["available"] is True
    assert named["codex_cli"]["binary"] == str(exe)

    globbed = {
        h["adapter"]: h
        for h in harness_status({"codex_cli": str(tmp_path / "*" / "codex.exe")})
    }
    assert globbed["codex_cli"]["available"] is True


def test_harness_status_without_overrides_still_asks_path():
    from council.catalog import harness_status

    rows = {h["adapter"]: h for h in harness_status()}
    assert rows["codex_cli"]["binary"] == "codex"


# ---- why the opencode catalogue is empty -------------------------------
#
# One empty list used to mean four different things, and the catalogue reported all of
# them as "is opencode installed?". It said that about an opencode that was on PATH and
# listing 41 models a minute later, which sent the user hunting for it in WSL.


def listing(monkeypatch, *, found=True, run=None):
    """Call `opencode_listing` against a stubbed binary and subprocess."""
    from council import catalog

    catalog.opencode_listing.cache_clear()
    monkeypatch.setattr(catalog, "resolve_binary", lambda b: r"C:\bin\opencode.cmd")
    monkeypatch.setattr(catalog.Path, "is_file", lambda self: found)
    monkeypatch.setattr(catalog.subprocess, "run", run)
    try:
        return catalog.opencode_listing()
    finally:
        catalog.opencode_listing.cache_clear()


def _proc(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_a_timeout_is_not_reported_as_missing(monkeypatch):
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("opencode", 60)

    models, problem = listing(monkeypatch, run=timeout)
    assert models == ()
    assert "did not answer" in problem and "installed" in problem
    assert "not installed" not in problem


def test_a_missing_binary_says_so(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("nope")

    _, problem = listing(monkeypatch, found=False, run=missing)
    assert "not installed" in problem


def test_a_failing_command_quotes_what_it_said(monkeypatch):
    _, problem = listing(
        monkeypatch, run=lambda *a, **k: _proc(1, stderr="not authenticated")
    )
    assert "exited 1" in problem and "not authenticated" in problem


def test_an_empty_list_points_at_auth(monkeypatch):
    _, problem = listing(monkeypatch, run=lambda *a, **k: _proc(0, stdout="\n"))
    assert "auth login" in problem


def test_a_working_opencode_has_no_problem_to_report(monkeypatch):
    models, problem = listing(
        monkeypatch, run=lambda *a, **k: _proc(0, stdout="ollama-cloud/kimi-k2.6\n")
    )
    assert models == ("ollama-cloud/kimi-k2.6",)
    assert problem == ""
