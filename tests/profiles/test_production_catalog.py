"""Production profile catalog + fail-closed load (P3/P5-shaped; CAP roles)."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from coscientist.profiles import load as load_mod
from coscientist.profiles.catalog import ProductionProfileCatalog, ProfileToolCatalog
from coscientist.profiles.errors import (
    CatalogVersionError,
    ProfileLoadError,
    UnknownProfileError,
    UnknownToolError,
)
from coscientist.profiles.load import (
    list_production_profile_names,
    load_production_catalog,
    load_profile,
    load_tool_catalog,
    profile_from_yaml_text,
)
from coscientist.profiles.models import Profile


def test_tool_catalog_exact_version_and_unique() -> None:
    cat = load_tool_catalog()
    assert cat.catalog_version == 2
    assert cat.tools == (
        "read_artifact",
        "search_papers",
        "download_papers_from_search",
        "find_papers_in_db",
    )
    assert cat.contains("read_artifact")
    assert cat.contains("search_papers")
    assert not cat.contains("search_pubmed")


def test_tool_catalog_duplicate_tools_refused() -> None:
    with pytest.raises(ValidationError, match="unique") as ei:
        ProfileToolCatalog.model_validate_json(
            json.dumps({"catalog_version": 1, "tools": ["a", "a"]})
        )
    assert ei.value.errors()[0]["type"] == "value_error"


def test_tool_catalog_list_tools_via_model_validate_refused() -> None:
    with pytest.raises(ValidationError, match="tools") as ei:
        ProfileToolCatalog.model_validate({"catalog_version": 1, "tools": ["a"]})
    assert ei.value.errors()[0]["type"] == "tuple_type"


def test_production_catalog_lists_research_profiles() -> None:
    index = load_production_catalog()
    assert index.catalog_version == 1
    assert list_production_profile_names() == (
        "research",
        "research-deepseek-litsearch",
        "research-glm",
        "research-glm-litsearch",
        "research-glm-searchonly",
    )
    assert index.get("research") is not None
    assert index.get("research-glm") is not None
    assert index.get("research-glm-litsearch") is not None
    assert index.get("research-deepseek-litsearch") is not None
    assert index.get("research-glm-searchonly") is not None
    assert index.get("medicine") is None
    assert index.get("microfluidics") is None


def test_duplicate_profile_names_in_index_refused() -> None:
    entry = {
        "name": "research",
        "profile_version": 1,
        "resource": "research.v1.yaml",
        "tool_catalog_version": 2,
    }
    with pytest.raises(ValidationError, match="unique") as ei:
        ProductionProfileCatalog.model_validate_json(
            json.dumps({"catalog_version": 1, "profiles": [entry, entry]})
        )
    assert ei.value.errors()[0]["type"] == "value_error"


def test_load_research_profile_has_design_roles() -> None:
    profile = load_profile("research", expected_tool_catalog_version=2)
    assert profile.name == "research"
    assert profile.profile_version == 1
    assert profile.max_attempts == 3
    assert set(profile.roles) == {
        "tz_spec",
        "planner",
        "hypothesizer",
        "researcher",
        "critic",
        "executor-route",
        "reviewer",
        "report",
    }
    assert set(profile.routes) == {
        "spec",
        "hypotheses",
        "literature",
        "plan",
        "critic",
        "execute",
        "review",
    }
    assert profile.routes["spec"].role == "tz_spec"
    assert profile.routes["plan"].role == "planner"
    assert profile.routes["critic"].role == "critic"
    assert profile.routes["execute"].role == "executor-route"
    assert profile.routes["review"].role == "reviewer"
    assert profile.roles["critic"].tools == ()
    assert profile.roles["reviewer"].tools == ("read_artifact",)
    # Wired need-stage roles (DESIGN §7.1): hypothesizer is tool-free; the
    # researcher runs the literature allowlist through the planned surface.
    assert profile.routes["hypotheses"].role == "hypothesizer"
    assert profile.routes["literature"].role == "researcher"
    assert profile.roles["hypothesizer"].tools == ()
    assert profile.roles["researcher"].tools == (
        "search_papers",
        "download_papers_from_search",
        "find_papers_in_db",
    )
    assert profile.routes["hypotheses"].budget.max_llm_calls == 8
    assert profile.routes["literature"].budget.max_tool_calls == 8
    # report role: post-finalize report lane model (no route — the lane is CLI-side).
    assert profile.roles["report"].model == "gpt-4.1"
    assert profile.roles["report"].tools == ()
    assert profile.mcp_servers == ()
    assert profile.alembic == "disabled"
    assert Profile.model_validate_json(profile.model_dump_json()) == profile


def test_load_research_glm_profile_pins_glm_models() -> None:
    profile = load_profile("research-glm", expected_tool_catalog_version=2)
    assert profile.name == "research-glm"
    assert profile.profile_version == 1
    assert profile.max_attempts == 3
    assert set(profile.roles) == set(load_profile("research").roles)
    assert set(profile.routes) == set(load_profile("research").routes)
    # Same wiring as research.v1; only the role models differ (GLM provider).
    for role in ("tz_spec", "planner", "hypothesizer", "researcher", "critic", "executor-route", "reviewer"):
        assert profile.roles[role].model == "glm-4.7"
    assert profile.routes["spec"].role == "tz_spec"
    assert profile.routes["execute"].role == "executor-route"
    assert profile.routes["hypotheses"].role == "hypothesizer"
    assert profile.routes["literature"].role == "researcher"
    assert profile.roles["reviewer"].tools == ("read_artifact",)
    assert profile.mcp_servers == ()
    assert Profile.model_validate_json(profile.model_dump_json()) == profile


def test_load_research_glm_litsearch_profile_drops_download() -> None:
    """litsearch = research-glm minus download_papers_from_search (lit-server S3 401)."""
    profile = load_profile("research-glm-litsearch", expected_tool_catalog_version=2)
    glm = load_profile("research-glm", expected_tool_catalog_version=2)
    assert profile.name == "research-glm-litsearch"
    assert profile.profile_version == 1
    assert profile.max_attempts == 3
    assert set(profile.roles) == set(glm.roles)
    assert set(profile.routes) == set(glm.routes)
    assert profile.roles["researcher"].tools == ("search_papers", "find_papers_in_db")
    assert "download_papers_from_search" not in profile.roles["researcher"].tools
    # Everything except the researcher tool set mirrors research-glm (models,
    # budgets): pin every role model and every route budget so future drift
    # from research-glm fails here, not only in a live run.
    for role in ("tz_spec", "planner", "hypothesizer", "critic", "executor-route", "reviewer"):
        assert profile.roles[role].model == glm.roles[role].model
        assert profile.roles[role].tools == glm.roles[role].tools
    assert profile.roles["researcher"].model == glm.roles["researcher"].model
    assert profile.roles["report"].model == glm.roles["report"].model
    assert profile.roles["report"].tools == glm.roles["report"].tools
    assert profile.routes["literature"].role == "researcher"
    for route in ("spec", "hypotheses", "literature", "plan", "critic", "execute", "review"):
        assert profile.routes[route].budget == glm.routes[route].budget
    assert profile.mcp_servers == ()
    assert Profile.model_validate_json(profile.model_dump_json()) == profile


def test_load_research_deepseek_litsearch_profile_pins_deepseek() -> None:
    """deepseek = litsearch with DeepSeek role models (everything else pinned)."""
    profile = load_profile("research-deepseek-litsearch", expected_tool_catalog_version=2)
    lit = load_profile("research-glm-litsearch", expected_tool_catalog_version=2)
    assert profile.name == "research-deepseek-litsearch"
    assert profile.roles["researcher"].tools == ("search_papers", "find_papers_in_db")
    for role, glm_role in profile.roles.items():
        assert glm_role.model == "deepseek-v4-flash", role
        assert glm_role.tools == lit.roles[role].tools
    for route in ("spec", "hypotheses", "literature", "plan", "critic", "execute", "review"):
        assert profile.routes[route].budget == lit.routes[route].budget
    assert Profile.model_validate_json(profile.model_dump_json()) == profile


def test_unknown_profile_medicine_refused() -> None:
    with pytest.raises(UnknownProfileError, match="unknown profile") as ei:
        load_profile("medicine")
    assert "medicine" in str(ei.value)


def test_unknown_profile_microfluidics_refused() -> None:
    with pytest.raises(UnknownProfileError, match="unknown profile") as ei:
        load_profile("microfluidics")
    assert "microfluidics" in str(ei.value)


def test_unknown_profile_arbitrary_refused() -> None:
    with pytest.raises(UnknownProfileError, match="unknown profile") as ei:
        load_profile("not-a-real-profile")
    assert ei.value.args[0] == "unknown profile: 'not-a-real-profile'"


def test_tool_catalog_version_pin_mismatch_refused() -> None:
    with pytest.raises(CatalogVersionError, match="catalog_version mismatch") as ei:
        load_profile("research", expected_tool_catalog_version=99)
    assert "expected=99" in str(ei.value)


def test_tool_catalog_version_bool_refused() -> None:
    with pytest.raises(CatalogVersionError, match="exact int") as ei:
        load_profile("research", expected_tool_catalog_version=True)  # type: ignore[arg-type]
    assert "exact int" in str(ei.value)


def test_unknown_tool_in_profile_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Role tool not in exact catalog → UnknownToolError (no blanket allow)."""
    real_read = load_mod._read_data_text
    bad_yaml = """
name: research
profile_version: 1
max_attempts: 3
roles:
  tz_spec:
    model: gpt-4.1
    tools:
      - invented_tool
routes:
  spec:
    role: tz_spec
    budget:
      max_llm_calls: 1
      max_tokens: 100
      max_tool_calls: 0
mcp_servers: []
catalog_extensions: []
alembic: disabled
integrations: []
plugins: []
"""

    def _read(name: str) -> str:
        if name == "research.v1.yaml":
            return bad_yaml
        return real_read(name)

    monkeypatch.setattr(load_mod, "_read_data_text", _read)
    with pytest.raises(UnknownToolError, match="not in exact profile tool catalog") as ei:
        load_profile("research")
    assert "invented_tool" in str(ei.value)


def test_yaml_root_non_mapping_refused() -> None:
    with pytest.raises(ProfileLoadError, match="mapping") as ei:
        profile_from_yaml_text("- just\n- a\n- list\n")
    assert ei.value.args[0] == "profile root must be a mapping"


def test_direct_model_validate_list_tools_still_refused() -> None:
    """Guard: packaged path must not grow a list→tuple shim on model_validate."""
    raw = {
        "name": "research",
        "profile_version": 1,
        "max_attempts": 3,
        "roles": {"tz_spec": {"model": "gpt-4.1", "tools": []}},
        "routes": {
            "spec": {
                "role": "tz_spec",
                "budget": {"max_llm_calls": 1, "max_tokens": 100, "max_tool_calls": 0},
            }
        },
        "mcp_servers": [],
        "catalog_extensions": [],
        "alembic": "disabled",
        "integrations": [],
        "plugins": [],
    }
    with pytest.raises(ValidationError, match="tools") as ei:
        Profile.model_validate(raw)
    assert ei.value.errors()[0]["type"] == "tuple_type"
    assert profile_from_yaml_text(yaml.safe_dump(raw)).name == "research"
