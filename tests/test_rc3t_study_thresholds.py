"""RC3t (#98): Study thresholds required; typed refuse (no invent / clamp)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coscientist.api.studies import CreateStudyBody
from coscientist.core.claims.claims import compute_verdict_snapshots
from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.graph import GraphStore, default_execution_registry
from coscientist.core.claims.verdict import hypothesis_verdict
from coscientist.core.events.errors import CatalogError, StudyRequiredError
from coscientist.core.events.events import (
    STUDY_CREATED_SCHEMA_VERSION,
    STUDY_CREATED_THRESHOLDS_SINCE,
)
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import (
    HitlPolicy,
    LoopPredicate,
    LoopSpec,
    RequirementRef,
    Study,
    TaskSpec,
)
from coscientist.core.execution.inputs import verdict_binding_digest
from coscientist.core.execution.thresholds import (
    CONFIRMATIONS_NEEDED_GE,
    MAX_EXPANSIONS_GE,
    REFUTATIONS_NEEDED_GE,
    SUPPORTED_NEEDED_GE,
)
from coscientist.core.plan.loop_predicates import (
    hypothesis_verdict_scoped,
    question_disposition_scoped,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DL = datetime(2026, 1, 2, tzinfo=UTC)

_THRESHOLD_FIELDS = (
    "confirmations_needed",
    "refutations_needed",
    "refutation_priority",
    "supported_needed",
    "max_expansions",
)


def _study_kwargs(**over: object) -> dict:
    base: dict = {
        "study_id": "s",
        "profile": "research",
        "request": "q",
        "catalog_version": 3,
        "hitl_policy": HitlPolicy(),
        "deadline": _DL,
        "created_at": _NOW,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
        "require_hypotheses": False,
    }
    base.update(over)
    return base


def _v15_payload(**over: object) -> dict:
    base: dict = {
        "study_id": "s",
        "catalog_version": 3,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("field", list(_THRESHOLD_FIELDS), ids=list(_THRESHOLD_FIELDS))
def test_missing_threshold_refuses(field: str) -> None:
    """Study construct / upcast / verdict bind: missing threshold → typed refuse."""
    kw = _study_kwargs()
    del kw[field]
    with pytest.raises(ValidationError, match=field):
        Study(**kw)

    payload = _v15_payload()
    del payload[field]
    with pytest.raises(CatalogError, match=f"{field}_required"):
        upcast("StudyCreated", STUDY_CREATED_THRESHOLDS_SINCE, payload)


def test_study_created_v15_keeps_thresholds() -> None:
    v, out = upcast(
        "StudyCreated",
        STUDY_CREATED_THRESHOLDS_SINCE,
        {
            "study_id": "s",
            "catalog_version": 3,
            "require_hypotheses": True,
            "confirmations_needed": 3,
            "refutations_needed": 2,
            "refutation_priority": True,
            "supported_needed": 4,
            "max_expansions": 5,
        },
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert out["confirmations_needed"] == 3
    assert out["refutations_needed"] == 2
    assert out["refutation_priority"] is True
    assert out["supported_needed"] == 4
    assert out["max_expansions"] == 5


def test_hypothesis_verdict_requires_thresholds() -> None:
    """Thresholds required on Study / CreateStudyBody (feeds hypothesis_verdict)."""
    payload = _study_kwargs()
    del payload["refutations_needed"]
    with pytest.raises(ValidationError, match="refutations_needed"):
        Study.model_validate(payload)
    body: dict[str, object] = {
        "profile": "research",
        "request": "q",
        "catalog_version": 3,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
    }
    with pytest.raises(ValidationError, match="refutations_needed"):
        CreateStudyBody.model_validate(body)


def test_hypothesis_verdict_no_clamp() -> None:
    """RC3t: supported_needed=1 is refused (no clamp to 2)."""
    with pytest.raises(CatalogError, match="supported_needed_must_be_ge_2"):
        hypothesis_verdict(
            [],
            False,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=1,
            confirmations_needed=1,
        )


def _verdict_call(
    *,
    refutations_needed: object = 1,
    refutation_priority: object = False,
    supported_needed: object = 2,
    confirmations_needed: object = 1,
) -> Callable[[], object]:
    """Typed refuse fixture: object kwargs → hypothesis_verdict (runtime checks)."""

    def _call() -> object:
        return hypothesis_verdict(
            [],
            False,
            refutations_needed=refutations_needed,
            refutation_priority=refutation_priority,
            supported_needed=supported_needed,
            confirmations_needed=confirmations_needed,
        )

    return _call


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (
            _verdict_call(refutations_needed=True),
            "refutations_needed_must_be_int_got_bool",
        ),
        (
            _verdict_call(refutations_needed=0),
            "refutations_needed_must_be_ge_1",
        ),
        (
            _verdict_call(refutation_priority=1),
            "refutation_priority_must_be_bool_got_int",
        ),
        (
            _verdict_call(supported_needed=True),
            "supported_needed_must_be_int_got_bool",
        ),
        (
            _verdict_call(supported_needed=1),
            "supported_needed_must_be_ge_2",
        ),
        (
            _verdict_call(confirmations_needed=True),
            "confirmations_needed_must_be_int_got_bool",
        ),
        (
            _verdict_call(confirmations_needed=0),
            "confirmations_needed_must_be_ge_1",
        ),
    ],
    ids=[
        "refutations_bool",
        "refutations_ge",
        "priority_int",
        "supported_bool",
        "supported_ge",
        "confirmations_bool",
        "confirmations_ge",
    ],
)
def test_hypothesis_verdict_typed_refuse(make: Callable[[], object], match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        make()


def test_compute_verdict_snapshots_study_required() -> None:
    g = GraphStore(default_execution_registry())
    with pytest.raises(StudyRequiredError, match="study_required"):
        compute_verdict_snapshots(g, study_id="s", study=None)


def test_legacy_v2_missing_refutation_priority_refuses() -> None:
    with pytest.raises(CatalogError, match="refutation_priority_required"):
        upcast(
            "StudyCreated",
            2,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": 1,
                "require_hypotheses": False,
            },
        )


@pytest.mark.parametrize(
    ("version", "payload", "match"),
    [
        (
            2,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": True,
                "refutation_priority": False,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v3:refutations_needed_must_be_int_got_bool",
        ),
        (
            2,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": 0,
                "refutation_priority": False,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v3:refutations_needed_must_be_ge_1",
        ),
        (
            2,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": 1,
                "refutation_priority": 1,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v3:refutation_priority_must_be_bool_got_int",
        ),
        (
            3,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": 1,
                "refutation_priority": False,
                "confirmations_needed": True,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v4:confirmations_needed_must_be_int_got_bool",
        ),
        (
            3,
            {
                "study_id": "s",
                "catalog_version": 3,
                "refutations_needed": 1,
                "refutation_priority": False,
                "confirmations_needed": 0,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v4:confirmations_needed_must_be_ge_1",
        ),
        (
            5,
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": True,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v6:supported_needed_must_be_int_got_bool",
        ),
        (
            5,
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 1,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v6:supported_needed_must_be_ge_2",
        ),
    ],
    ids=[
        "v3_refutations_bool",
        "v3_refutations_ge",
        "v3_priority_int",
        "v4_confirmations_bool",
        "v4_confirmations_ge",
        "v6_supported_bool",
        "v6_supported_ge",
    ],
)
def test_upcast_threshold_typed_refuse(version: int, payload: dict, match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        upcast("StudyCreated", version, payload)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_replans": True,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v7:max_replans_must_be_int_got_bool",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v7:max_expansions_required",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": True,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v7:max_expansions_must_be_int_got_bool",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": -1,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v7:max_expansions_must_be_ge_0",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_replans": -1,
                "require_hypotheses": False,
            },
            r"StudyCreated\.v7:max_expansions_must_be_ge_0",
        ),
    ],
    ids=[
        "v7_max_replans_bool",
        "v7_max_expansions_required",
        "v7_max_expansions_bool",
        "v7_max_expansions_ge",
        "v7_max_replans_ge",
    ],
)
def test_upcast_v7_max_expansions_refuse(payload: dict, match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        upcast("StudyCreated", 6, payload)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": True,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            r"StudyCreated\.v15:confirmations_needed_must_be_int_got_bool",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 0,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            r"StudyCreated\.v15:confirmations_needed_must_be_ge_1",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": True,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            r"StudyCreated\.v15:refutations_needed_must_be_int_got_bool",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 1,
                "max_expansions": 2,
            },
            r"StudyCreated\.v15:supported_needed_must_be_ge_2",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": -1,
            },
            r"StudyCreated\.v15:max_expansions_must_be_ge_0",
        ),
        (
            {
                "study_id": "s",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": 1,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            r"StudyCreated\.v15:refutation_priority_must_be_bool_got_int",
        ),
    ],
    ids=[
        "v15_confirmations_bool",
        "v15_confirmations_ge",
        "v15_refutations_bool",
        "v15_supported_ge",
        "v15_max_expansions_ge",
        "v15_priority_int",
    ],
)
def test_upcast_v15_threshold_typed_refuse(payload: dict, match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        upcast("StudyCreated", STUDY_CREATED_THRESHOLDS_SINCE, payload)


def test_max_expansions_from_max_replans_rename_only() -> None:
    v, out = upcast(
        "StudyCreated",
        6,
        {
            "study_id": "s",
            "catalog_version": 3,
            "confirmations_needed": 1,
            "refutations_needed": 1,
            "refutation_priority": False,
            "supported_needed": 2,
            "max_replans": 4,
            "require_hypotheses": False,
        },
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert out["max_expansions"] == 4


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": True,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "confirmations_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": "2",
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "confirmations_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": True,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "refutations_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": "2",
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "refutations_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": True,
                "max_expansions": 2,
            },
            "supported_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": "2",
                "max_expansions": 2,
            },
            "supported_needed",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": True,
            },
            "max_expansions",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": "2",
            },
            "max_expansions",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": 1,
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "refutation_priority",
        ),
        (
            {
                "profile": "research",
                "request": "q",
                "catalog_version": 3,
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": "true",
                "supported_needed": 2,
                "max_expansions": 2,
            },
            "refutation_priority",
        ),
    ],
    ids=[
        "confirmations_bool",
        "confirmations_str",
        "refutations_bool",
        "refutations_str",
        "supported_bool",
        "supported_str",
        "max_expansions_bool",
        "max_expansions_str",
        "priority_int",
        "priority_str",
    ],
)
def test_study_thresholds_strict_refuse_coercion(body: dict, match: str) -> None:
    """StrictInt/StrictBool: True/\"2\" must not coerce into thresholds."""
    with pytest.raises(ValidationError, match=match):
        Study.model_validate(
            {
                "study_id": "s",
                "hitl_policy": HitlPolicy(),
                "deadline": _DL,
                "created_at": _NOW,
                **body,
            }
        )
    with pytest.raises(ValidationError, match=match):
        CreateStudyBody.model_validate(body)


def test_ge_constants_match_study_fields() -> None:
    """Single source: thresholds.py GE constants feed Study Field(ge=…)."""
    assert CONFIRMATIONS_NEEDED_GE == 1
    assert REFUTATIONS_NEEDED_GE == 1
    assert SUPPORTED_NEEDED_GE == 2
    assert MAX_EXPANSIONS_GE == 0
    fields = Study.model_fields
    assert fields["confirmations_needed"].metadata[0].ge == CONFIRMATIONS_NEEDED_GE
    assert fields["refutations_needed"].metadata[0].ge == REFUTATIONS_NEEDED_GE
    assert fields["supported_needed"].metadata[0].ge == SUPPORTED_NEEDED_GE
    assert fields["max_expansions"].metadata[0].ge == MAX_EXPANSIONS_GE


def test_verdict_binding_digest_thresholds_required() -> None:
    with pytest.raises(CatalogError, match="refutations_needed_required"):
        verdict_binding_digest(ref="Q1", snapshot={"refutation_priority": False})
    with pytest.raises(CatalogError, match="refutation_priority_required"):
        verdict_binding_digest(ref="Q1", snapshot={"refutations_needed": 1})
    with pytest.raises(CatalogError, match="refutation_priority_must_be_bool_got_int"):
        verdict_binding_digest(
            ref="Q1",
            snapshot={"refutations_needed": 1, "refutation_priority": 1},
        )
    with pytest.raises(ValueError, match="provenance required"):
        verdict_binding_digest(
            ref="Q1",
            snapshot={
                "refutations_needed": 1,
                "refutation_priority": False,
            },
        )


def _minimal_loop() -> LoopSpec:
    return LoopSpec(
        loop_id="L1",
        body=[
            TaskSpec(
                task_key="BODY",
                title="BODY",
                goal="loop body",
                route="experiment",
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
            )
        ],
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        max_iter=1,
        scope_hypotheses=["h1"],
        scope_questions=["Q1"],
    )


def test_loop_predicates_study_required() -> None:
    rt = SimpleNamespace(study=None)
    loop = _minimal_loop()
    with pytest.raises(StudyRequiredError, match="study_required"):
        hypothesis_verdict_scoped(rt, loop, "h1", upto_iter=0)
    with pytest.raises(StudyRequiredError, match="study_required"):
        question_disposition_scoped(rt, loop, "Q1", upto_iter=0)


def test_dispositions_propagates_study_required() -> None:
    """RC3t: study=None with claim nodes → StudyRequiredError (public API)."""
    from coscientist.core.claims.graph import GraphNode

    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id="s:claim:c1",
            kind="claim",
            study_id="s",
            ref_id="c1",
            attrs={},
        )
    )
    with pytest.raises(StudyRequiredError, match="study_required"):
        dispositions(
            tasks={},
            plans={"p1": {"questions": [{"id": "Q1", "text": "q?"}]}},
            graph=g,
            study=None,
        )


def test_dispositions_propagates_other_catalog_error() -> None:
    """Fail-closed: bool ``k`` override → CatalogError via public dispositions()."""
    from coscientist.core.claims.graph import GraphEdge, GraphNode
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    sid = "s_disp_ce"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=question_node_id(sid, "Q1"),
            kind="question",
            study_id=sid,
            ref_id="Q1",
            attrs={"formulation": "q?"},
        )
    )
    aid = f"{sid}/E#1"
    derived = {
        "criterion_status": "met",
        "evidence_kind": "computational",
        "frozen": True,
        "lit_own_criterion": False,
        "accepted": True,
        "artifact_id": "A1",
        "claim_outcome": "consistent",
        "check_role": "prediction",
        "checked_against": "value_match",
        "sesoi": 0.5,
        "uses_sesoi": True,
        "used_sesoi": True,
        "predicted_direction": "positive",
        "effect": {
            "est": 1.5,
            "ci_low": 1.5,
            "ci_high": 1.5,
            "producer": "agent",
            "trusted": False,
            "core_evaluated": True,
            "countable": True,
            "produced_by_call_id": f"{aid}:1",
        },
    }
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=_NOW,
        graph=g,
        derived=derived,
    )
    assert drops == []
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            g.add_edge(GraphEdge.model_validate(ev.payload))

    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=_DL,
        created_at=_NOW,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
    )
    # ``k`` overrides confirmations_needed; bool must refuse (no invent / coerce).
    with pytest.raises(CatalogError, match="confirmations_needed_must_be_int_got_bool"):
        dispositions(
            tasks={},
            plans={"p1": {"questions": [{"id": "Q1", "text": "q?"}]}},
            graph=g,
            study=study,
            results={aid: {"outcome": "SUCCESS"}},
            k=True,
        )
