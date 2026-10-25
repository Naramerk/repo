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
    assert cat.catalog_version == 1
    assert cat.tools == ("read_artifact",)
    assert cat.contains("read_artifact")
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


def test_production_catalog_lists_research_only() -> None:
    index = load_production_catalog()
    assert index.catalog_version == 1
    assert list_production_profile_names() == ("research",)
    assert index.get("research") is not None
    assert index.get("medicine") is None
    assert index.get("microfluidics") is None


def test_duplicate_profile_names_in_index_refused() -> None:
    entry = {
        "name": "research",
        "profile_version": 1,
        "resource": "research.v1.yaml",
        "tool_catalog_version": 1,
    }
    with pytest.raises(ValidationError, match="unique") as ei:
        ProductionProfileCatalog.model_validate_json(
            json.dumps({"catalog_version": 1, "profiles": [entry, entry]})
        )
    assert ei.value.errors()[0]["type"] == "value_error"


def test_load_research_profile_has_design_roles() -> None:
    profile = load_profile("research", expected_tool_catalog_version=1)
    assert profile.name == "research"
    assert profile.profile_version == 1
    assert profile.max_attempts == 3
    assert set(profile.roles) == {
        "tz_spec",
        "planner",
        "critic",
        "executor-route",
        "reviewer",
    }
    assert set(profile.routes) == {"spec", "plan", "critic", "execute", "review"}
    assert profile.routes["spec"].role == "tz_spec"
    assert profile.routes["plan"].role == "planner"
    assert profile.routes["critic"].role == "critic"
    assert profile.routes["execute"].role == "executor-route"
    assert profile.routes["review"].role == "reviewer"
    assert profile.roles["critic"].tools == ()
    assert profile.roles["reviewer"].tools == ("read_artifact",)
    assert profile.mcp_servers == ()
    assert profile.alembic == "disabled"
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
