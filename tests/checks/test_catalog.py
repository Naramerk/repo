"""Catalog loader validation (DESIGN §5.2)."""

from __future__ import annotations

import pytest

from coscientist.checks.catalog import load_catalog, minimal_test_catalog
from coscientist.core.events.errors import CatalogError
from coscientist.core.claims.graph import default_execution_registry


def test_minimal_catalog_loads():
    cat = minimal_test_catalog()
    assert cat.catalog_version == "0.1.0"
    assert all(t.text_ru for t in cat.templates)


def test_unknown_kind_errors():
    with pytest.raises(CatalogError, match="unknown CodeCheck kind"):
        load_catalog(
            {
                "catalog_version": "1",
                "templates": [
                    {
                        "id": "x",
                        "title": "x",
                        "text_ru": "текст",
                        "method_sources": ["m"],
                        "applies_to": ["experiment"],
                        "check": {"kind": "not_a_real_kind"},
                    }
                ],
                "defaults": {"experiment": ["x"]},
            }
        )


def test_unknown_node_kind_with_registry():
    reg = default_execution_registry()
    with pytest.raises(CatalogError, match="unknown node kind"):
        load_catalog(
            {
                "catalog_version": "1",
                "templates": [
                    {
                        "id": "x",
                        "title": "x",
                        "text_ru": "текст",
                        "method_sources": ["m"],
                        "applies_to": ["not_in_schema"],
                        "check": {"kind": "artifact_exists", "output": "main"},
                    }
                ],
                "defaults": {"not_in_schema": ["x"]},
            },
            kind_registry=reg,
        )


def test_missing_text_ru():
    with pytest.raises(CatalogError, match="text_ru"):
        load_catalog(
            {
                "catalog_version": "1",
                "templates": [
                    {
                        "id": "x",
                        "title": "x",
                        "method_sources": ["m"],
                        "applies_to": ["experiment"],
                        "check": {"kind": "artifact_exists"},
                    }
                ],
                "defaults": {},
            }
        )


def test_text_ru_and_check_same_source():
    cat = minimal_test_catalog()
    t = next(x for x in cat.templates if x.id == "value_match")
    assert "Значение" in t.text_ru
    assert t.check.kind == "value_matches"  # type: ignore[union-attr]
