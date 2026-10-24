"""Tiny catalog fixture for unit tests (not the scientific ontology).

Lives under ``tests/`` — production ``coscientist.checks.catalog`` hosts only the
loader, never a fixture catalog.
"""

from __future__ import annotations

from typing import Any

from coscientist.checks.catalog import (
    CATALOG_VERSION,
    PROPOSED_CHECK_TEMPLATE_ID,
    TEMPLATE_VERSION,
    load_catalog,
)
from coscientist.core.execution.domain import Catalog
from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA

PLAN_N_CRITIC_TEMPLATE_IDS = frozenset(
    r.template_id for r in PLAN_N_CRITIC_CRITERIA
)


def _tmpl(
    *,
    id: str,
    title: str,
    text_ru: str,
    check: dict[str, Any],
    role: str,
    rule_ids: list[str],
    method_sources: list[str],
    applies_to: list[str],
    required: bool,
    version: int,
    uses_sesoi: bool,
    predicts_null: bool,
    predicted_direction: str,
    exempt_from_freeze: bool,
    sesoi: float | None = None,
    sesoi_min: float | None = None,
    sesoi_max: float | None = None,
) -> dict[str, Any]:
    """Build a fully-typed minimal_test_catalog template row (all keys explicit)."""
    check_row = dict(check)
    # No security-toggle defaults — caller / CodeCheck load requires explicit bools.
    row: dict[str, Any] = {
        "id": id,
        "title": title,
        "text_ru": text_ru,
        "rule_ids": rule_ids,
        "method_sources": method_sources,
        "applies_to": applies_to,
        "check": check_row,
        "role": role,
        "required": required,
        "version": version,
        "uses_sesoi": uses_sesoi,
        "predicts_null": predicts_null,
        "predicted_direction": predicted_direction,
        "exempt_from_freeze": exempt_from_freeze,
    }
    if sesoi is not None:
        row["sesoi"] = sesoi
    if sesoi_min is not None:
        row["sesoi_min"] = sesoi_min
    if sesoi_max is not None:
        row["sesoi_max"] = sesoi_max
    return row


def _tmpl_validity(
    *,
    id: str,
    title: str,
    text_ru: str,
    check: dict[str, Any],
    method_sources: list[str],
    exempt_from_freeze: bool,
) -> dict[str, Any]:
    """Validity-row helper for minimal_test_catalog (explicit required meta)."""
    return _tmpl(
        id=id,
        title=title,
        text_ru=text_ru,
        check=check,
        role="validity",
        rule_ids=["R23"],
        method_sources=method_sources,
        applies_to=["experiment"],
        required=True,
        version=TEMPLATE_VERSION,
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
        exempt_from_freeze=exempt_from_freeze,
    )


def minimal_test_catalog() -> Catalog:
    """Tiny catalog for M0 unit tests (not the scientific ontology).

    ``catalog_version`` is ``CATALOG_VERSION`` (StrictInt generation).
    """
    from coscientist.core.claims.graph import default_execution_registry

    return load_catalog(
        {
            "catalog_version": CATALOG_VERSION,
            "edition": "M0 test",
            "sources_sha256": {"test": "abc"},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "reviewed-rules",
                    "excerpt": "UNASSESSED separate",
                    "digest": "abc",
                }
            ],
            "templates": [
                _tmpl_validity(
                    id="schema_ok",
                    title="Schema valid",
                    text_ru="Артефакт проходит schema_valid.",
                    check={
                        "kind": "schema_valid",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    method_sources=["test"],
                    exempt_from_freeze=False,
                ),
                _tmpl_validity(
                    id="artifact_present",
                    title="Artifact exists",
                    text_ru="Требуемый артефакт должен существовать.",
                    check={
                        "kind": "artifact_exists",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    method_sources=["test"],
                    exempt_from_freeze=False,
                ),
                _tmpl(
                    id="value_match",
                    title="Value matches",
                    text_ru="Значение в артефакте совпадает с целевым.",
                    check={
                        "kind": "value_matches",
                        "output": "main",
                        "pointer": "/count",
                        "target": 225,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    role="prediction",
                    rule_ids=["R23"],
                    method_sources=["test"],
                    applies_to=["experiment"],
                    required=True,
                    version=TEMPLATE_VERSION,
                    uses_sesoi=False,
                    predicts_null=False,
                    predicted_direction="two_sided",
                    exempt_from_freeze=False,
                ),
                _tmpl(
                    id="value_match_sesoi",
                    title="Effect exceeds SESOI",
                    text_ru="Эффект превышает SESOI в предсказанном направлении.",
                    check={
                        "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": ">",
                        "target": 0,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    role="prediction",
                    rule_ids=["R23"],
                    method_sources=["test"],
                    applies_to=["experiment"],
                    required=True,
                    version=TEMPLATE_VERSION,
                    uses_sesoi=True,
                    sesoi=0.5,
                    sesoi_min=0.0,
                    sesoi_max=100.0,
                    predicts_null=False,
                    predicted_direction="positive",
                    exempt_from_freeze=False,
                ),
                _tmpl(
                    id="tost_equivalence",
                    title="TOST equivalence (null prediction)",
                    text_ru="Эффект эквивалентен нулю по TOST (CI внутри SESOI).",
                    check={
                        "kind": "metric_threshold",
                        "output": "main",
                        "metric": "effect",
                        "pointer": "/effect",
                        "op": "==",
                        "target": 0,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    role="prediction",
                    rule_ids=["R23"],
                    method_sources=["test"],
                    applies_to=["experiment"],
                    required=True,
                    version=TEMPLATE_VERSION,
                    uses_sesoi=True,
                    sesoi=0.5,
                    sesoi_min=0.0,
                    sesoi_max=100.0,
                    predicts_null=True,
                    predicted_direction="two_sided",
                    exempt_from_freeze=False,
                ),
                _tmpl_validity(
                    id="human_review",
                    title="Human/LLM review",
                    text_ru="Ревьюер оценивает качество результата.",
                    check={
                        "reviewer_role": "reviewer",
                        "question": "Is the result adequate?",
                    },
                    method_sources=["test"],
                    exempt_from_freeze=False,
                ),
                _tmpl_validity(
                    id="rubric_check",
                    title="Rubric (qualitative deliverable)",
                    text_ru="Артефакт удовлетворяет замороженной рубрике плана.",
                    check={
                        "kind": "rubric",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
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
                    method_sources=["test"],
                    exempt_from_freeze=False,
                ),
                _tmpl(
                    id=PROPOSED_CHECK_TEMPLATE_ID,
                    title="Agent-proposed check",
                    text_ru=(
                        "Проверка, предложенная агентом, когда ни один шаблон "
                        "каталога не покрывает задачу (требует HITL-одобрения)."
                    ),
                    method_sources=["DESIGN_M1 §16"],
                    rule_ids=["R23"],
                    applies_to=["experiment"],
                    check={
                        "kind": "artifact_exists",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    role="prediction",
                    required=True,
                    version=TEMPLATE_VERSION,
                    uses_sesoi=False,
                    predicts_null=False,
                    predicted_direction="two_sided",
                    exempt_from_freeze=False,
                ),
                *[
                    _tmpl_validity(
                        id=tid,
                        title=tid,
                        text_ru=f"M1 PLAN-n critic marker {tid}.",
                        method_sources=["DESIGN_M1 §4"],
                        check={
                            "reviewer_role": "critic",
                            "question": f"marker:{tid}",
                        },
                        exempt_from_freeze=True,
                    )
                    for tid in sorted(PLAN_N_CRITIC_TEMPLATE_IDS)
                ],
            ],
            "defaults": {"experiment": ["artifact_present"]},
            "code_output_names": ["code", "script", "program", "model", "source"],
        },
        kind_registry=default_execution_registry(),
    )
