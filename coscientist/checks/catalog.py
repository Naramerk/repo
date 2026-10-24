"""Requirement catalog loader (DESIGN §5). Unknown kind / role → CatalogError."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coscientist.core.claims.graph import KindRegistry
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    RequirementTemplate,
    ReviewCheck,
    RuleRef,
    TemplateKind,
)

KNOWN_CODE_KINDS = frozenset(
    {
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
        "quote_in_source",
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
        "rubric",
    }
)
KNOWN_REVIEW_ROLES = frozenset({"reviewer", "critic", "human"})


def _migrate_code_check(raw: dict[str, Any]) -> dict[str, Any]:
    """Upcast legacy CodeCheck fields (holdout_output → holdout required)."""
    out = dict(raw)
    kind = out.get("kind")
    if kind == "metric_on_holdout":
        if out.get("holdout_output") and not out.get("holdout"):
            raise CatalogError(
                "metric_on_holdout: legacy holdout_output removed — provide "
                "catalog holdout{{...}} + holdout_digest (agent slots never supply labels)"
            )
        out.pop("holdout_output", None)
    return out


def _parse_check(raw: dict[str, Any]) -> CodeCheck | ReviewCheck:
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in KNOWN_CODE_KINDS:
            raise CatalogError(f"unknown CodeCheck kind: {kind}")
        return CodeCheck.model_validate(_migrate_code_check(raw))
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
        uses_sesoi = bool(t["uses_sesoi"]) if "uses_sesoi" in t else False
        sesoi = t.get("sesoi")
        sesoi_min = t.get("sesoi_min")
        sesoi_max = t.get("sesoi_max")
        predicts_null = bool(t["predicts_null"]) if "predicts_null" in t else False
        if "predicted_direction" in t:
            direction = t["predicted_direction"]
        elif role == "prediction" and (
            bool(t.get("uses_sesoi")) or bool(t.get("predicts_null"))
        ):
            raise CatalogError(
                f"template {t.get('id')!r} sesoi/null prediction requires predicted_direction"
            )
        else:
            direction = "two_sided"
        if direction not in ("positive", "negative", "two_sided"):
            raise CatalogError(
                f"template {t.get('id')!r} bad predicted_direction {direction!r}"
            )
        if "min_power" in t:
            raise CatalogError(
                f"template {t.get('id')!r}: min_power is not supported in M1 "
                "(deferred until the core computes power)"
            )
        try:
            sesoi_f = float(sesoi) if sesoi is not None else None
            sesoi_min_f = float(sesoi_min) if sesoi_min is not None else None
            sesoi_max_f = float(sesoi_max) if sesoi_max is not None else None
        except (TypeError, ValueError) as e:
            raise CatalogError(
                f"template {t.get('id')!r}: sesoi bounds must be numeric"
            ) from e
        if uses_sesoi:
            if sesoi_f is None:
                raise CatalogError(
                    f"template {t.get('id')!r}: uses_sesoi requires sesoi"
                )
            if sesoi_min_f is not None and sesoi_f < sesoi_min_f:
                raise CatalogError(
                    f"template {t.get('id')!r}: sesoi {sesoi_f} < sesoi_min {sesoi_min_f}"
                )
            if sesoi_max_f is not None and sesoi_f > sesoi_max_f:
                raise CatalogError(
                    f"template {t.get('id')!r}: sesoi {sesoi_f} > sesoi_max {sesoi_max_f}"
                )
        elif sesoi_f is not None:
            raise CatalogError(
                f"template {t.get('id')!r}: sesoi set but uses_sesoi is false"
            )
        if predicts_null and not uses_sesoi:
            raise CatalogError(
                f"template {t.get('id')!r}: predicts_null requires uses_sesoi"
            )
        if "kind" not in t:
            raise CatalogError(f"template {t.get('id')!r}: missing kind")
        kind_raw = t["kind"]
        try:
            tmpl_kind = TemplateKind(kind_raw)
        except ValueError as e:
            raise CatalogError(
                f"template {t.get('id')!r}: unknown kind {kind_raw!r}"
            ) from e
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
                kind=tmpl_kind,
                required=bool(t["required"]) if "required" in t else True,
                version=str(t["version"]) if "version" in t else "1",
                uses_sesoi=uses_sesoi,
                sesoi=sesoi_f,
                sesoi_min=sesoi_min_f,
                sesoi_max=sesoi_max_f,
                predicts_null=predicts_null,
                predicted_direction=direction,
            )
        )

    if "defaults" not in data:
        raise CatalogError("catalog missing defaults")
    defaults_raw = data["defaults"]
    if not isinstance(defaults_raw, dict):
        raise CatalogError("catalog defaults must be dict")
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

    if "code_output_names" in data:
        raw_outs = data["code_output_names"]
        if not isinstance(raw_outs, list):
            raise CatalogError("code_output_names must be list")
        code_outputs = [str(x) for x in raw_outs]
    else:
        code_outputs = []
    edition = str(data["edition"]) if "edition" in data else ""
    sources = dict(data["sources_sha256"]) if "sources_sha256" in data else {}
    return Catalog(
        catalog_version=str(data["catalog_version"]),
        edition=edition,
        sources_sha256=sources,
        rules=rules,
        templates=templates,
        defaults=defaults,
        code_output_names=code_outputs,
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
                    "id": "schema_ok",
                    "title": "Schema valid",
                    "text_ru": "Артефакт проходит schema_valid.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "schema_valid", "output": "main"},
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                },
                {
                    "id": "artifact_present",
                    "title": "Artifact exists",
                    "text_ru": "Требуемый артефакт должен существовать.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "artifact_exists", "output": "main"},
                    "role": "validity",
                    "kind": "catalog",
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
                    "kind": "catalog",
                    "required": True,
                    "predicted_direction": "two_sided",
                },
                {
                    "id": "value_match_sesoi",
                    "title": "Effect exceeds SESOI",
                    "text_ru": "Эффект превышает SESOI в предсказанном направлении.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": ">",
                        "target": 0,
                    },
                    "role": "prediction",
                    "kind": "catalog",
                    "required": True,
                    "uses_sesoi": True,
                    "sesoi": 0.5,
                    "sesoi_min": 0.0,
                    "sesoi_max": 100.0,
                    "predicts_null": False,
                    "predicted_direction": "positive",
                },
                {
                    "id": "tost_equivalence",
                    "title": "TOST equivalence (null prediction)",
                    "text_ru": "Эффект эквивалентен нулю по TOST (CI внутри SESOI).",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": "==",
                        "target": 0,
                    },
                    "role": "prediction",
                    "kind": "catalog",
                    "required": True,
                    "uses_sesoi": True,
                    "sesoi": 0.5,
                    "sesoi_min": 0.0,
                    "sesoi_max": 100.0,
                    "predicts_null": True,
                    "predicted_direction": "two_sided",
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
                    "kind": "catalog",
                    "required": True,
                },
                {
                    "id": "rubric_check",
                    "title": "Rubric (qualitative deliverable)",
                    "text_ru": "Артефакт удовлетворяет замороженной рубрике плана.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "rubric",
                        "output": "main",
                        "rubric": {
                            "items": [
                                {
                                    "id": "exists",
                                    "text": "Deliverable artifact exists",
                                    "required": True,
                                    "check": "code",
                                    "code_kind": "artifact_exists",
                                }
                            ],
                            "aggregate": "all_required",
                        },
                    },
                    # validity only — never feeds claim stance (PR13 invariant)
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                },
                {
                    # Meta-template: body comes from an approved CheckProposal (PR20).
                    # Sentinel check is never evaluated; evaluate.py special-cases this id.
                    "id": "proposed_check",
                    "title": "Agent-proposed check",
                    "text_ru": (
                        "Проверка, предложенная агентом, когда ни один шаблон "
                        "каталога не покрывает задачу (требует HITL-одобрения)."
                    ),
                    "rule_ids": ["R23"],
                    "method_sources": ["DESIGN_M1 §16"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "artifact_exists",
                        "output": "main",
                    },
                    "role": "prediction",
                    "kind": "proposed",
                    "required": True,
                    "predicted_direction": "two_sided",
                },
                # PLAN-n critic markers (kind=plan_n_critic). Valid only on Task.plan_role=plan_n.
                *[
                    {
                        "id": tid,
                        "title": tid,
                        "text_ru": f"M1 PLAN-n critic marker {tid}.",
                        "rule_ids": ["R23"],
                        "method_sources": ["DESIGN_M1 §4"],
                        "applies_to": ["plan"],
                        "check": {"kind": "artifact_exists", "output": "main"},
                        "role": "validity",
                        "kind": "plan_n_critic",
                        "required": True,
                    }
                    for tid in (
                        "plan_expansion_schema",
                        "plan_expansion_depth_quota_reserve",
                        "plan_expansion_no_refutation_escape",
                        "plan_expansion_acceptance_criteria",
                        "plan_expansion_no_duplicates",
                    )
                ],
            ],
            "defaults": {"experiment": ["artifact_present"]},
            # Only catalog-declared names are code for independence; else DATA.
            "code_output_names": ["code", "script", "program", "model", "source"],
        }
    )
