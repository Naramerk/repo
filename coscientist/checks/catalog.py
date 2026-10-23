"""Requirement catalog loader (DESIGN §5). Unknown kind / role → CatalogError."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coscientist.core.domain import (
    Catalog,
    CodeCheck,
    RequirementTemplate,
    ReviewCheck,
    RuleRef,
)
from coscientist.core.errors import CatalogError
from coscientist.core.graph import KindRegistry

KNOWN_CODE_KINDS = frozenset(
    {
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
    }
)
KNOWN_REVIEW_ROLES = frozenset({"reviewer", "critic", "human"})


def _parse_check(raw: dict[str, Any]) -> CodeCheck | ReviewCheck:
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in KNOWN_CODE_KINDS:
            raise CatalogError(f"unknown CodeCheck kind: {kind}")
        return CodeCheck.model_validate(raw)
    if "reviewer_role" in raw:
        role = raw["reviewer_role"]
        if role not in KNOWN_REVIEW_ROLES:
            raise CatalogError(f"unknown reviewer_role: {role}")
        return ReviewCheck.model_validate(raw)
    raise CatalogError("check must be CodeCheck (kind) or ReviewCheck (reviewer_role)")


def load_catalog(
    data: dict[str, Any] | Path | str,
    *,
    kind_registry: KindRegistry | None = None,
) -> Catalog:
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    rules = [RuleRef.model_validate(r) for r in data.get("rules", [])]
    templates: list[RequirementTemplate] = []
    for t in data.get("templates", []):
        if not t.get("text_ru"):
            raise CatalogError(f"template {t.get('id')!r} missing text_ru")
        if not t.get("method_sources") and not t.get("rule_ids"):
            raise CatalogError(
                f"template {t.get('id')!r} needs method_sources or rule_ids"
            )
        check = _parse_check(t["check"])
        applies = list(t.get("applies_to", []))
        if kind_registry is not None:
            for k in applies:
                try:
                    kind_registry.require_node_kind(k)
                except KeyError as e:
                    raise CatalogError(str(e)) from e
        role = t.get("role")
        if role not in ("prediction", "validity"):
            raise CatalogError(
                f"template {t.get('id')!r} needs role prediction|validity, got {role!r}"
            )
        templates.append(
            RequirementTemplate(
                id=t["id"],
                title=t["title"],
                text_ru=t["text_ru"],
                method_sources=list(t.get("method_sources", [])),
                rule_ids=list(t.get("rule_ids", [])),
                applies_to=applies,
                check=check,
                role=role,
                required=bool(t.get("required", True)),
                version=str(t.get("version", "1")),
            )
        )

    defaults_raw = data.get("defaults", {})
    defaults = {str(k): list(v) for k, v in defaults_raw.items()}
    if kind_registry is not None:
        for k in defaults:
            try:
                kind_registry.require_node_kind(k)
            except KeyError as e:
                raise CatalogError(str(e)) from e

    # every RuleRef covered by ≥1 template
    covered = {rid for t in templates for rid in t.rule_ids}
    for rule in rules:
        if rule.rule_id not in covered:
            raise CatalogError(f"RuleRef {rule.rule_id} not covered by any template")

    # every default key must list known templates
    tmpl_ids = {t.id for t in templates}
    for kind, ids in defaults.items():
        for tid in ids:
            if tid not in tmpl_ids:
                raise CatalogError(f"defaults[{kind}] unknown template {tid}")

    return Catalog(
        catalog_version=str(data["catalog_version"]),
        edition=str(data.get("edition", "")),
        sources_sha256=dict(data.get("sources_sha256", {})),
        rules=rules,
        templates=templates,
        defaults=defaults,
    )


def minimal_test_catalog() -> Catalog:
    """Tiny catalog for M0 unit tests (not the scientific ontology)."""
    return load_catalog(
        {
            "catalog_version": "0.1.0",
            "edition": "M0 test",
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "reviewed-rules",
                    "excerpt": "UNASSESSED separate",
                    "digest": "abc",
                }
            ],
            "templates": [
                {
                    "id": "artifact_present",
                    "title": "Artifact exists",
                    "text_ru": "Требуемый артефакт должен существовать.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "artifact_exists", "output": "main"},
                    "role": "validity",
                    "required": True,
                },
                {
                    "id": "value_match",
                    "title": "Value matches",
                    "text_ru": "Значение в артефакте совпадает с целевым.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "value_matches",
                        "output": "main",
                        "pointer": "/count",
                        "target": 225,
                    },
                    "role": "prediction",
                    "required": True,
                },
                {
                    "id": "human_review",
                    "title": "Human/LLM review",
                    "text_ru": "Ревьюер оценивает качество результата.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "reviewer_role": "reviewer",
                        "question": "Is the result adequate?",
                    },
                    "role": "validity",
                    "required": True,
                },
            ],
            "defaults": {"experiment": ["artifact_present"]},
        }
    )
