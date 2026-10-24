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
_PREDICTED_DIRECTIONS = frozenset({"positive", "negative", "two_sided"})


def _require_nonempty_str(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise CatalogError(f"catalog_meta_v1:{key}_required")
    v = data[key]
    if not isinstance(v, str) or not v.strip():
        raise CatalogError(
            f"catalog_meta_v1:{key}_required_str_got_{type(v).__name__}"
        )
    return v.strip()


def _require_template_bool(t: dict[str, Any], key: str, tid: object) -> bool:
    if key not in t:
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_required")
    v = t[key]
    if not isinstance(v, bool):
        raise CatalogError(
            f"catalog_template_v1:{tid}:{key}_must_be_bool_got_{type(v).__name__}"
        )
    return v


def _require_template_str(t: dict[str, Any], key: str, tid: object) -> str:
    if key not in t:
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_required")
    v = t[key]
    if not isinstance(v, str) or not v.strip():
        raise CatalogError(
            f"catalog_template_v1:{tid}:{key}_must_be_str_got_{type(v).__name__}"
        )
    return v.strip()


def _require_list(data: dict[str, Any], key: str, *, where: str) -> list[Any]:
    if key not in data:
        raise CatalogError(f"{where}:{key}_required")
    v = data[key]
    if not isinstance(v, list):
        raise CatalogError(f"{where}:{key}_must_be_list_got_{type(v).__name__}")
    return v


def _optional_float(raw: object, *, what: str) -> float | None:
    if raw is None:
        return None
    if type(raw) is bool or not isinstance(raw, (int, float)):
        raise CatalogError(f"{what}_must_be_numeric_got_{type(raw).__name__}")
    return float(raw)  # audit-ok[coerce_float]: coerce after int|float type gate (bool rejected)


def _validate_code_check(raw: dict[str, Any]) -> dict[str, Any]:
    """Strict CodeCheck dict: no legacy field migration / silent pop."""
    out = dict(raw)
    if "allow_network" not in out:
        raise CatalogError(
            f"CodeCheck.allow_network required (template check kind={out.get('kind')!r})"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        )
    if "holdout_output" in out:
        raise CatalogError(
            "metric_on_holdout: legacy holdout_output refused — provide "
            "catalog holdout{...} + holdout_digest (catalog_version bump; no silent pop)"
        )
    return out


SUPPORTED_CATALOG_VERSIONS = frozenset({"1.0.0"})
CURRENT_CATALOG_VERSION = "1.0.0"


def _parse_check(raw: dict[str, Any]) -> CodeCheck | ReviewCheck:
    """Require explicit ``check_type`` (code|review); never infer from key presence."""
    if "check_type" not in raw:
        raise CatalogError("check_type required (code|review); key-presence inference removed")
    ctype = raw["check_type"]
    body = {k: v for k, v in raw.items() if k != "check_type"}
    if ctype == "code":
        if "kind" not in body:
            raise CatalogError("CodeCheck.kind required when check_type=code")
        kind = body["kind"]
        if kind not in KNOWN_CODE_KINDS:
            raise CatalogError(f"unknown CodeCheck kind: {kind}")
        return CodeCheck.model_validate(_validate_code_check(body))
    if ctype == "review":
        if "reviewer_role" not in body:
            raise CatalogError("ReviewCheck.reviewer_role required when check_type=review")
        role = body["reviewer_role"]
        if role not in KNOWN_REVIEW_ROLES:
            raise CatalogError(f"unknown reviewer_role: {role}")
        if "max_calls" not in body:
            raise CatalogError(
                f"ReviewCheck.max_calls required (reviewer_role={role!r})"
            )
        return ReviewCheck.model_validate(body)
    raise CatalogError(f"check_type must be code|review, got {ctype!r}")


def load_catalog(
    data: dict[str, Any] | Path | str,
    *,
    kind_registry: KindRegistry | None = None,
) -> Catalog:
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogError(
            f"catalog root must be object, got {type(data).__name__}"
        )

    catalog_version_early = _require_nonempty_str(data, "catalog_version")
    if catalog_version_early not in SUPPORTED_CATALOG_VERSIONS:
        raise CatalogError(
            f"catalog_version={catalog_version_early!r} refused; "
            f"supported={sorted(SUPPORTED_CATALOG_VERSIONS)} "
            f"(current={CURRENT_CATALOG_VERSION})"
        )

    rules = [
        RuleRef.model_validate(r)
        for r in _require_list(data, "rules", where="catalog_meta_v1")
    ]
    templates = []
    for t in _require_list(data, "templates", where="catalog_meta_v1"):
        if not isinstance(t, dict):
            raise CatalogError("catalog_template_v1:entry_must_be_object")
        if not t.get("text_ru"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            raise CatalogError(f"template {t.get('id')!r} missing text_ru")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        tid = t.get("id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        method_sources = _require_list(
            t, "method_sources", where=f"catalog_template_v1:{tid}"
        )
        rule_ids = _require_list(t, "rule_ids", where=f"catalog_template_v1:{tid}")
        if not method_sources and not rule_ids:
            raise CatalogError(
                f"template {tid!r} needs method_sources or rule_ids"
            )
        check = _parse_check(t["check"])
        applies = list(
            _require_list(t, "applies_to", where=f"catalog_template_v1:{tid}")
        )
        if kind_registry is not None:
            for k in applies:
                try:
                    kind_registry.require_node_kind(k)
                except KeyError as e:
                    raise CatalogError(str(e)) from e
        role = t.get("role")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if role not in ("prediction", "validity"):
            raise CatalogError(
                f"template {tid!r} needs role prediction|validity, got {role!r}"
            )
        uses_sesoi = _require_template_bool(t, "uses_sesoi", tid)
        predicts_null = _require_template_bool(t, "predicts_null", tid)
        required = _require_template_bool(t, "required", tid)
        version = _require_template_str(t, "version", tid)
        direction = t.get("predicted_direction")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if direction not in _PREDICTED_DIRECTIONS:
            raise CatalogError(
                f"catalog_template_v1:{tid}:predicted_direction_required_got_{direction!r}"
            )
        if "min_power" in t:
            raise CatalogError(
                f"template {tid!r}: min_power is not supported in M1 "
                "(deferred until the core computes power)"
            )
        sesoi_f = _optional_float(t.get("sesoi"), what=f"template {tid!r}:sesoi")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        sesoi_min_f = _optional_float(
            t.get("sesoi_min"), what=f"template {tid!r}:sesoi_min"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        )
        sesoi_max_f = _optional_float(
            t.get("sesoi_max"), what=f"template {tid!r}:sesoi_max"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        )
        if uses_sesoi:
            if sesoi_f is None:
                raise CatalogError(
                    f"template {tid!r}: uses_sesoi requires sesoi"
                )
            if sesoi_min_f is not None and sesoi_f < sesoi_min_f:
                raise CatalogError(
                    f"template {tid!r}: sesoi {sesoi_f} < sesoi_min {sesoi_min_f}"
                )
            if sesoi_max_f is not None and sesoi_f > sesoi_max_f:
                raise CatalogError(
                    f"template {tid!r}: sesoi {sesoi_f} > sesoi_max {sesoi_max_f}"
                )
        elif sesoi_f is not None:
            raise CatalogError(
                f"template {tid!r}: sesoi set but uses_sesoi is false"
            )
        if predicts_null and not uses_sesoi:
            raise CatalogError(
                f"template {tid!r}: predicts_null requires uses_sesoi"
            )
        if "kind" not in t:
            raise CatalogError(f"template {tid!r}: missing kind")
        kind_raw = t["kind"]
        try:
            tmpl_kind = TemplateKind(kind_raw)
        except ValueError as e:
            raise CatalogError(
                f"template {tid!r}: unknown kind {kind_raw!r}"
            ) from e
        templates.append(
            RequirementTemplate(
                id=t["id"],
                title=t["title"],
                text_ru=t["text_ru"],
                method_sources=list(method_sources),
                rule_ids=list(rule_ids),
                applies_to=applies,
                check=check,
                role=role,
                kind=tmpl_kind,
                required=required,
                version=version,
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
    defaults: dict[str, list[Any]] = {}  # audit-ok[field_default]: structural empty/collector; not invented domain value
    for k, v in defaults_raw.items():
        if not isinstance(k, str) or not k:
            raise CatalogError(
                f"defaults key must be non-empty str, got {type(k).__name__}"
            )
        if not isinstance(v, list):
            raise CatalogError(
                f"defaults[{k!r}] must be list, got {type(v).__name__}"
            )
        defaults[k] = list(v)
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
        code_outputs = []
        for x in raw_outs:
            if not isinstance(x, str) or not x:
                raise CatalogError(
                    f"code_output_names entries must be non-empty str, got {type(x).__name__}"
                )
            code_outputs.append(x)
    else:
        code_outputs = []
    catalog_version = catalog_version_early
    edition = _require_nonempty_str(data, "edition")
    if "sources_sha256" not in data:
        raise CatalogError("catalog_meta_v1:sources_sha256_required")
    sources_raw = data["sources_sha256"]
    if not isinstance(sources_raw, dict):
        raise CatalogError(
            f"catalog_meta_v1:sources_sha256_must_be_object_got_{type(sources_raw).__name__}"
        )
    sources: dict[str, str] = {}  # audit-ok[field_default]: structural empty/collector; not invented domain value
    for k, v in sources_raw.items():
        if not isinstance(k, str) or not k:
            raise CatalogError(
                f"sources_sha256 key must be non-empty str, got {type(k).__name__}"
            )
        if not isinstance(v, str) or not v:
            raise CatalogError(
                f"sources_sha256[{k!r}] must be non-empty str, got {type(v).__name__}"
            )
        sources[k] = v
    return Catalog(
        catalog_version=catalog_version,
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
            "catalog_version": "1.0.0",
            "edition": "M0 test",
            "sources_sha256": {},
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
                    "check": {"check_type": "code", "kind": "schema_valid", "output": "main",
                        "allow_network": False,
                    },
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                },
                {
                    "id": "artifact_present",
                    "title": "Artifact exists",
                    "text_ru": "Требуемый артефакт должен существовать.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"check_type": "code", "kind": "artifact_exists", "output": "main",
                        "allow_network": False,
                    },
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                },
                {
                    "id": "value_match",
                    "title": "Value matches",
                    "text_ru": "Значение в артефакте совпадает с целевым.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"check_type": "code", "kind": "value_matches",
                        "output": "main",
                        "pointer": "/count",
                        "target": 225,
                        "allow_network": False,
                    },
                    "role": "prediction",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                },
                {
                    "id": "value_match_sesoi",
                    "title": "Effect exceeds SESOI",
                    "text_ru": "Эффект превышает SESOI в предсказанном направлении.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"check_type": "code", "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": ">",
                        "target": 0,
                        "allow_network": False,
                    },
                    "role": "prediction",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
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
                    "check": {"check_type": "code", "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": "==",
                        "target": 0,
                        "allow_network": False,
                    },
                    "role": "prediction",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
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
                    "check": {"check_type": "review", "reviewer_role": "reviewer",
                        "question": "Is the result adequate?",
                        "max_calls": 1,
                    },
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                },
                {
                    "id": "rubric_check",
                    "title": "Rubric (qualitative deliverable)",
                    "text_ru": "Артефакт удовлетворяет замороженной рубрике плана.",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"check_type": "code", "kind": "rubric",
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
                        "allow_network": False,
                    },
                    # validity only — never feeds claim stance (PR13 invariant)
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
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
                    "check": {"check_type": "code", "kind": "artifact_exists",
                        "output": "main",
                        "allow_network": False,
                    },
                    "role": "prediction",
                    "kind": "proposed",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
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
                        "check": {
                            "check_type": "code",
                            "kind": "artifact_exists",
                            "output": "main",
                            "allow_network": False,
                        },
                        "role": "validity",
                        "kind": "plan_n_critic",
                        "required": True,
                        "version": "1",
                        "uses_sesoi": False,
                        "predicts_null": False,
                        "predicted_direction": "two_sided",
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
