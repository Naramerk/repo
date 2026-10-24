"""Catalog loader validation (DESIGN §5.2)."""

from __future__ import annotations

import pytest

from coscientist.checks.catalog import load_catalog, minimal_test_catalog
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.errors import CatalogError


def _strict_template(**overrides: object) -> dict:
    """Minimal template row with required fail-closed meta fields."""
    row: dict = {
        "id": "x",
        "title": "x",
        "text_ru": "текст",
        "method_sources": ["m"],
        "applies_to": ["experiment"],
        "check": {"kind": "artifact_exists", "output": "main"},
        "role": "validity",
        "kind": "catalog",
        "required": True,
        "version": "1",
        "uses_sesoi": False,
        "predicts_null": False,
        "predicted_direction": "two_sided",
    }
    row.update(overrides)
    return row


def _strict_catalog(**overrides: object) -> dict:
    data: dict = {
        "catalog_version": "1",
        "edition": "test",
        "sources_sha256": {},
        "templates": [_strict_template()],
        "defaults": {"experiment": ["x"]},
    }
    data.update(overrides)
    return data


def test_minimal_catalog_loads():
    cat = minimal_test_catalog()
    assert cat.catalog_version == "0.1.0"
    assert all(t.text_ru for t in cat.templates)
    assert isinstance(cat.sources_sha256, dict)


def test_unknown_kind_errors():
    with pytest.raises(CatalogError, match="unknown CodeCheck kind"):
        load_catalog(
            _strict_catalog(
                templates=[_strict_template(check={"kind": "not_a_real_kind"})],
            )
        )


def test_unknown_node_kind_with_registry():
    reg = default_execution_registry()
    with pytest.raises(CatalogError, match="unknown node kind"):
        load_catalog(
            _strict_catalog(
                templates=[_strict_template(applies_to=["not_in_schema"])],
                defaults={"not_in_schema": ["x"]},
            ),
            kind_registry=reg,
        )


def test_missing_text_ru():
    with pytest.raises(CatalogError, match="text_ru"):
        load_catalog(
            _strict_catalog(
                templates=[
                    {
                        "id": "x",
                        "title": "x",
                        "method_sources": ["m"],
                        "applies_to": ["experiment"],
                        "check": {"kind": "artifact_exists"},
                    }
                ],
                defaults={},
            )
        )


def test_text_ru_and_check_same_source():
    cat = minimal_test_catalog()
    t = next(x for x in cat.templates if x.id == "value_match")
    assert "Значение" in t.text_ru
    assert t.check.kind == "value_matches"  # type: ignore[union-attr]


def test_load_catalog_requires_strict_template_bools_and_version():
    """Missing uses_sesoi / soft bools / version → CatalogError (no silent defaults)."""
    base_t = {
        "id": "x",
        "title": "x",
        "text_ru": "текст",
        "method_sources": ["m"],
        "applies_to": ["experiment"],
        "check": {"kind": "artifact_exists", "output": "main"},
        "role": "validity",
        "kind": "catalog",
    }
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:uses_sesoi_required"
    ):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "test",
                "sources_sha256": {},
                "templates": [base_t],
                "defaults": {},
            }
        )
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:required_must_be_bool"
    ):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "test",
                "sources_sha256": {},
                "templates": [
                    {
                        **base_t,
                        "uses_sesoi": False,
                        "predicts_null": False,
                        "predicted_direction": "two_sided",
                        "required": "false",
                        "version": "1",
                    }
                ],
                "defaults": {},
            }
        )
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:version_required"
    ):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "test",
                "sources_sha256": {},
                "templates": [
                    {
                        **base_t,
                        "uses_sesoi": False,
                        "predicts_null": False,
                        "predicted_direction": "two_sided",
                        "required": True,
                    }
                ],
                "defaults": {},
            }
        )


def test_load_catalog_requires_edition_and_sources_sha256():
    """No silent edition=\"\" / missing sources_sha256 — refuse with catalog_meta_v1."""
    with pytest.raises(CatalogError, match="catalog_meta_v1:edition_required"):
        load_catalog(
            {
                "catalog_version": "1",
                "sources_sha256": {},
                "templates": [],
                "defaults": {},
            }
        )
    with pytest.raises(CatalogError, match="catalog_meta_v1:edition_required"):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "",
                "sources_sha256": {},
                "templates": [],
                "defaults": {},
            }
        )
    with pytest.raises(
        CatalogError, match="catalog_meta_v1:sources_sha256_required"
    ):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "test",
                "templates": [],
                "defaults": {},
            }
        )
    with pytest.raises(
        CatalogError, match="catalog_meta_v1:sources_sha256_must_be_object"
    ):
        load_catalog(
            {
                "catalog_version": "1",
                "edition": "test",
                "sources_sha256": None,
                "templates": [],
                "defaults": {},
            }
        )
    with pytest.raises(
        CatalogError, match="catalog_meta_v1:catalog_version_required"
    ):
        load_catalog(
            {
                "catalog_version": "  ",
                "edition": "test",
                "sources_sha256": {},
                "templates": [],
                "defaults": {},
            }
        )
