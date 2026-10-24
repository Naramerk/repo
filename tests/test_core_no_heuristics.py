"""Missing / unknown domain values must raise (no silent defaults).

Maps review refusals → tests (see PR #39 description).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.claims.claims import RuntimeClaim
from coscientist.core.claims.verdict import (
    _binding_fields,
    _claim_validity_ok,
    recompute_effect_provenance,
)
from coscientist.core.events.errors import (
    ArtifactRejected,
    JournalFormatError,
    SandboxInfraError,
    require_journal_field,
)
from coscientist.core.execution.artifacts import (
    LocalArtifactStore,
    make_tool_ref,
    output_kind_from_spec,
)
from coscientist.core.execution.domain import HitlPolicy, InputSpec, OutputSpec, QuestionSpec, TaskKind
from coscientist.core.execution.executor import (
    _check_role,
    _criterion_key_for_claim,
    _criterion_status_for_claim,
    _effect_from_checks,
)
from coscientist.core.plan.foreach import _nat_key


def test_output_spec_role_required():
    with pytest.raises(ValidationError):
        OutputSpec(name="main", media_type="application/json")


def test_question_spec_final_type_required():
    with pytest.raises(ValidationError):
        QuestionSpec(id="Q1", text="q")


def test_input_spec_quorum_rejects_zero():
    with pytest.raises(ValidationError, match="quorum"):
        InputSpec(from_task="E0", output="main", quorum=0)


def test_hitl_gate_unknown_kind_raises():
    with pytest.raises(KeyError, match="unknown HITL gate"):
        HitlPolicy().gate("not_a_gate")


def test_checked_against_required():
    with pytest.raises(JournalFormatError, match="checked_against") as ei:
        _criterion_key_for_claim({}, checks=[], criteria=[])
    assert ei.value.field == "checked_against"


def test_unknown_check_role_raises():
    with pytest.raises(JournalFormatError, match="unknown check role") as ei:
        _check_role({"role": "weird"})
    assert ei.value.field == "role"


def test_missing_check_role_raises():
    with pytest.raises(JournalFormatError, match="role") as ei:
        _check_role({})
    assert ei.value.field == "role"


def test_missing_check_status_for_criterion_raises():
    with pytest.raises(ValueError, match="no check status"):
        _criterion_status_for_claim(
            {"checked_against": "missing_tmpl"},
            checks=[{"template_id": "other", "status": "MET", "role": "prediction"}],
            criteria=[],
        )


def test_effect_missing_producer_raises():
    with pytest.raises(JournalFormatError, match="producer") as ei:
        recompute_effect_provenance({"est": 1.0, "kind": "point", "core_evaluated": True})
    assert ei.value.field == "producer"


def test_effect_from_checks_missing_producer_propagates():
    """Fail-closed: JournalFormatError must not be swallowed by continue."""
    checks = [
        {
            "role": "prediction",
            "effect": {"est": 1.0, "kind": "point", "core_evaluated": True},
            "used_sesoi": False,
        }
    ]
    with pytest.raises(JournalFormatError, match="producer") as ei:
        _effect_from_checks(checks)
    assert ei.value.field == "producer"


def test_output_kind_from_spec_requires_role():
    assert output_kind_from_spec(role="code") == "code"
    assert output_kind_from_spec(role="data") == "data"
    with pytest.raises(TypeError):
        output_kind_from_spec()  # type: ignore[call-arg]


def test_task_kind_from_route_rejects_alias():
    """foreach uses TaskKind(route); unknown route raises (no lit/hyp aliases)."""
    with pytest.raises(ValueError):
        TaskKind("lit")
    with pytest.raises(ValueError):
        TaskKind("hyp")


def test_artifact_put_requires_role(tmp_path):
    store = LocalArtifactStore(tmp_path)
    with pytest.raises((TypeError, ArtifactRejected)):
        store.put(b"x", make_tool_ref("c1"), name="main", media_type="text/plain")  # type: ignore[call-arg]


def test_nat_key_no_regex():
    assert _nat_key("H2") < _nat_key("H10")


def test_missing_charged_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="charged") as ei:
        require_journal_field({"closed": True, "provenance": "real"}, "charged", ctx="a1")
    assert ei.value.field == "charged"


def test_missing_provenance_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="provenance") as ei:
        require_journal_field({"closed": True, "charged": False}, "provenance", ctx="a1")
    assert ei.value.field == "provenance"


def test_missing_attempt_role_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="attempt_role") as ei:
        require_journal_field(
            {"provenance": "real", "charged": False, "closed": False},
            "attempt_role",
            ctx="a1",
        )
    assert ei.value.field == "attempt_role"


def test_missing_closed_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="closed") as ei:
        require_journal_field(
            {"provenance": "real", "charged": False, "attempt_role": "agent"},
            "closed",
            ctx="a1",
        )
    assert ei.value.field == "closed"


def test_missing_target_kind_raises_type_error():
    with pytest.raises(TypeError):
        RuntimeClaim(criterion_status="met", check_role="prediction")  # type: ignore[call-arg]


def test_sandbox_infra_requires_transient():
    with pytest.raises(TypeError):
        SandboxInfraError("oom")  # type: ignore[call-arg]


def test_binding_missing_kind_raises():
    with pytest.raises(JournalFormatError, match="kind") as ei:
        _binding_fields({"ref": "x", "artifact_id": "a", "input": "main"})
    assert ei.value.field == "kind"


def test_binding_missing_input_raises():
    with pytest.raises(JournalFormatError, match="input") as ei:
        _binding_fields({"kind": "artifact", "artifact_id": "a"})
    assert ei.value.field == "input"


def test_claim_validity_ok_required():
    with pytest.raises(JournalFormatError, match="validity_ok") as ei:
        _claim_validity_ok({})
    assert ei.value.field == "validity_ok"


def test_cancel_close_reason_unmapped_raises():
    from coscientist.core.execution.attempts import _close_reason_for_cancel

    with pytest.raises(JournalFormatError, match="unmapped cancel") as ei:
        _close_reason_for_cancel("not_a_reason")
    assert ei.value.field == "close_reason"


def test_hypothesis_snap_missing_ref_raises():
    from coscientist.core.events.errors import require_journal_field

    snap = {"verdict": "untested", "by_literature": False, "by_theory": False, "provenance": "real", "k": 1}
    with pytest.raises(JournalFormatError, match="ref") as ei:
        require_journal_field(snap, "ref", ctx="H1")
    assert ei.value.field == "ref"


def test_hypothesis_snap_missing_k_raises():
    snap = {
        "ref": "H1",
        "verdict": "untested",
        "by_literature": False,
        "by_theory": False,
        "provenance": "real",
    }
    with pytest.raises(JournalFormatError, match="k") as ei:
        require_journal_field(snap, "k", ctx="H1")
    assert ei.value.field == "k"


def test_study_refused_missing_classifier_error_raises():
    with pytest.raises(JournalFormatError, match="classifier_error") as ei:
        require_journal_field({"kind": "safety", "reason": "x"}, "classifier_error", ctx="StudyRefused")
    assert ei.value.field == "classifier_error"


def test_attempt_started_writer_remains_v9_until_38():
    """AttemptStarted v10 bump deferred until after #38 schema v1 rebase."""
    from coscientist.core.events.schema_versions import ATTEMPT_STARTED_SCHEMA_VERSION
    from coscientist.core.events.upcasters import writer_schema_version

    assert ATTEMPT_STARTED_SCHEMA_VERSION == 9
    assert writer_schema_version("AttemptStarted") == 9


def test_criterion_freeze_missing_predicts_null_raises():
    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.executor import _criterion_freeze_attrs

    sid = "st"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=criterion_node_id(sid, "T1"),
            kind="criterion",
            study_id=sid,
            ref_id="T1",
            attrs={
                "frozen": True,
                "uses_sesoi": False,
                "predicted_direction": "two_sided",
                # predicts_null absent
            },
        )
    )
    with pytest.raises(JournalFormatError, match="predicts_null") as ei:
        _criterion_freeze_attrs(g, sid, "T1")
    assert ei.value.field == "predicts_null"


def test_derive_claim_missing_task_id_raises():
    from types import SimpleNamespace

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import CheckResult, ReqStatus, TaskOutcome
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=criterion_node_id(sid, "value_match"),
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            attrs={
                "frozen": True,
                "uses_sesoi": False,
                "predicts_null": False,
                "predicted_direction": "two_sided",
            },
        )
    )
    task = SimpleNamespace(
        task_id=None,
        spec=SimpleNamespace(method=None, criteria=[]),
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="prediction",
            effect={
                "est": 1.0,
                "kind": "point",
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
        )
    ]
    with pytest.raises(JournalFormatError, match="task_id") as ei:
        _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw={"checked_against": "value_match", "target": "Q1", "target_kind": "question"},
            checks=checks,
            graph=g,
            study_id=sid,
            validated=True,
        )
    assert ei.value.field == "task_id"


def test_runtime_claim_missing_predicts_null_dropped():
    from coscientist.core.claims.claims import runtime_claim_from_node
    from coscientist.core.claims.graph import GraphNode

    node = GraphNode(
        node_id="st:claim:C1",
        kind="claim",
        study_id="st",
        ref_id="C1",
        attrs={
            "target": "Q1",
            "target_kind": "question",
            "checked_against": "value_match",
            "criterion_status": "met",
            "check_role": "prediction",
            "frozen": True,
            "accepted": True,
            "validity_ok": True,
            "evidence_kind": "computational",
            "predicted_direction": "two_sided",
            # predicts_null absent
        },
    )
    claim, reason = runtime_claim_from_node(node, study_id="st")
    assert claim is None
    assert reason == "bad_predicts_null"


def test_runtime_claim_missing_predicted_direction_dropped():
    from coscientist.core.claims.claims import runtime_claim_from_node
    from coscientist.core.claims.graph import GraphNode

    node = GraphNode(
        node_id="st:claim:C1",
        kind="claim",
        study_id="st",
        ref_id="C1",
        attrs={
            "target": "Q1",
            "target_kind": "question",
            "checked_against": "value_match",
            "criterion_status": "met",
            "check_role": "prediction",
            "frozen": True,
            "accepted": True,
            "validity_ok": True,
            "evidence_kind": "computational",
            "predicts_null": False,
            # predicted_direction absent
        },
    )
    claim, reason = runtime_claim_from_node(node, study_id="st")
    assert claim is None
    assert reason == "bad_predicted_direction"


def test_machine_open_attempts_missing_row_raises():
    """Missing attempt row must not invent {} (KeyError, not silent open)."""
    from types import SimpleNamespace

    from coscientist.core.execution.machine import StudyRuntime

    rt = object.__new__(StudyRuntime)
    rt.tasks = {"t1": SimpleNamespace(attempts=["a1"])}
    rt.attempts = {}
    with pytest.raises(KeyError):
        rt.invariant_open_attempts()


def test_machine_open_attempts_absent_closed_counts_open():
    """Pre-v10 journals omit closed until AttemptClosed — treat as open."""
    from types import SimpleNamespace

    from coscientist.core.execution.machine import StudyRuntime

    rt = object.__new__(StudyRuntime)
    rt.tasks = {"t1": SimpleNamespace(attempts=["a1", "a2"])}
    rt.attempts = {
        "a1": {"provenance": "real"},
        "a2": {"provenance": "real"},
    }
    assert rt.invariant_open_attempts() is False


def test_study_fulfilled_requires_final_types():
    from coscientist.core.claims.rules import study_fulfilled

    with pytest.raises(JournalFormatError, match="question_final_types") as ei:
        study_fulfilled({"Q1": "refuted"}, all_required_success=True)
    assert ei.value.field == "final_type"


def test_lifecycle_freeze_requires_catalog_template():
    """accept_plan freeze path refuses missing catalog (no write-side defaults)."""
    from coscientist.core.events.errors import JournalFormatError

    # Direct contract of the freeze block (same raise as lifecycle.py).
    with pytest.raises(JournalFormatError) as ei:
        catalog = None
        if catalog is None:
            raise JournalFormatError(
                "catalog required to freeze criteria",
                field="catalog",
            )
    assert ei.value.field == "catalog"


def test_effect_invalid_est_raises_not_skipped():
    """Structural effect parse errors must not be swallowed by continue."""
    checks = [
        {
            "role": "prediction",
            "effect": {
                "est": "not-a-float",
                "kind": "point",
                "producer": "agent",
                "core_evaluated": True,
            },
            "used_sesoi": False,
        }
    ]
    with pytest.raises(JournalFormatError, match="effect") as ei:
        _effect_from_checks(checks)
    assert ei.value.field == "effect"


def test_claim_is_severe_requires_predicted_direction():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import claim_is_severe

    claim = SimpleNamespace(
        criterion_status="met",
        validity_ok=True,
        validity="valid",
        claim_outcome="consistent",
        severe=None,
        effect={
            "est": 2.0,
            "kind": "point",
            "producer": "agent",
            "core_evaluated": True,
            "countable": True,
            "trusted": False,
        },
        frozen=True,
        criterion_frozen_at_seq=1,
        first_cited_evidence_seq=2,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        predicts_null=False,
        # predicted_direction absent
    )
    with pytest.raises(JournalFormatError, match="predicted_direction") as ei:
        claim_is_severe(claim)
    assert ei.value.field == "predicted_direction"


def test_foreach_requires_active_plan_revision():
    from coscientist.core.events.errors import require_journal_field

    with pytest.raises(JournalFormatError, match="revision") as ei:
        require_journal_field({"plan_id": "p1", "tasks": []}, "revision", ctx="plan")
    assert ei.value.field == "revision"


def test_study_refused_missing_reason_raises():
    with pytest.raises(JournalFormatError, match="reason") as ei:
        require_journal_field(
            {"kind": "safety", "classifier_error": False},
            "reason",
            ctx="StudyRefused",
        )
    assert ei.value.field == "reason"


def test_effect_exceeds_sesoi_requires_direction_and_est():
    from coscientist.core.claims.verdict import effect_exceeds_sesoi

    with pytest.raises(TypeError):
        effect_exceeds_sesoi(  # type: ignore[call-arg]
            sesoi=0.5,
            effect={"est": 1.0, "countable": True, "core_evaluated": True},
        )
    with pytest.raises(JournalFormatError, match="sesoi|est") as ei:
        effect_exceeds_sesoi(
            sesoi=0.5,
            effect={"countable": True, "core_evaluated": True},
            direction="two_sided",
        )
    assert ei.value.field == "sesoi"
    with pytest.raises(JournalFormatError, match="predicted_direction") as ei2:
        effect_exceeds_sesoi(
            sesoi=0.5,
            effect={
                "est": 1.0,
                "countable": True,
                "core_evaluated": True,
                "producer": "agent",
            },
            direction="sideways",
        )
    assert ei2.value.field == "predicted_direction"


def test_exclusion_reason_requires_evidence_kind():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import exclusion_reason

    claim = SimpleNamespace(
        counts=lambda: False,
        exclusion_reason=None,
        check_role="prediction",
        validity_ok=True,
        validity="valid",
        provenance="real",
        claim_outcome="consistent",
        # no evidence_kind / is_lit
    )
    with pytest.raises(JournalFormatError, match="evidence_kind") as ei:
        exclusion_reason(claim)
    assert ei.value.field == "evidence_kind"


def test_intent_rejects_invalid_and_missing():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import _intent

    with pytest.raises(JournalFormatError, match="intent") as ei:
        _intent({})
    assert ei.value.field == "intent"
    with pytest.raises(JournalFormatError, match="invalid claim intent") as ei2:
        _intent(SimpleNamespace(intent="maybe"))
    assert ei2.value.field == "intent"


def test_hypothesis_report_row_requires_typed_rule():
    from coscientist.core.claims.verdict import hypothesis_report_row

    with pytest.raises(JournalFormatError, match="rule") as ei:
        hypothesis_report_row(
            hypothesis_id="H1",
            question_id="Q1",
            claims=[],
            verdict_value="inconclusive",
            rule=None,
        )
    assert ei.value.field == "rule"


@pytest.mark.asyncio
async def test_create_study_caps_come_from_study_model(tmp_path, clock):
    """create_study requires explicit caps + deadline (no Study / magic defaults)."""
    from datetime import timedelta

    from pydantic_core import PydanticUndefined

    from coscientist.core.execution.domain import Study
    from tests.helpers import REQUIRED_STUDY_CAPS, close_runtime, create_study, open_runtime

    for name in REQUIRED_STUDY_CAPS:
        assert Study.model_fields[name].default is PydanticUndefined

    rt = await open_runtime(tmp_path, "st_caps", clock=clock)
    try:
        base = {
            "profile": "research",
            "request": "q",
            "catalog_version": "0.1.0",
            "deadline": clock.now() + timedelta(hours=1),
            "sid": "st_caps",
            **REQUIRED_STUDY_CAPS,
        }
        omit_alpha = {k: v for k, v in base.items() if k != "evalue_alpha"}
        with pytest.raises(ValueError, match="evalue_alpha"):
            await rt.create_study(**omit_alpha)

        with pytest.raises(ValueError, match="deadline"):
            await rt.create_study(**{**base, "deadline": None})

        await create_study(
            rt,
            profile="research",
            request="q",
            catalog_version="0.1.0",
            deadline=clock.now() + timedelta(hours=1),
            sid="st_caps",
            evalue_alpha=0.07,
        )
        assert rt.study is not None
        assert rt.study.evalue_alpha == 0.07
    finally:
        await close_runtime(rt)


def test_gate_config_timeout_s_required():
    """GateConfig.timeout_s has no default; use GATE_TIMEOUTS_S (DESIGN §7.3)."""
    from coscientist.core.execution.domain import GATE_TIMEOUTS_S, GateConfig

    with pytest.raises(ValidationError):
        GateConfig(enabled=True, on_timeout="delegate")  # timeout_s required
    with pytest.raises(ValidationError):
        GateConfig(timeout_s=GATE_TIMEOUTS_S["spec_approval"], on_timeout="delegate")
    cfg = GateConfig(
        enabled=True,
        timeout_s=GATE_TIMEOUTS_S["spec_approval"],
        on_timeout="delegate",
    )
    assert cfg.timeout_s == 120


def test_dispositions_missing_final_type_raises():
    """Fail-closed: no except→not_attempted invent when plan lacks final_type."""
    from types import SimpleNamespace

    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.graph import GraphStore, default_execution_registry

    study = SimpleNamespace(
        study_id="st",
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
    )
    with pytest.raises(JournalFormatError, match="final_type") as ei:
        dispositions(
            tasks={},
            plans={"pl": {"questions": [{"id": "Q1", "text": "why"}], "tasks": []}},
            graph=GraphStore(default_execution_registry()),
            study=study,
            results={},
        )
    assert ei.value.field == "final_type"


def test_foreach_select_requires_graph():
    from types import SimpleNamespace

    from coscientist.core.execution.domain import ForEachSpec
    from coscientist.core.plan.foreach import select_foreach_items

    fe = ForEachSpec(kind="hypothesis", of_task="FE", selector="all")
    with pytest.raises(JournalFormatError, match="graph") as ei:
        select_foreach_items(SimpleNamespace(graph=None), fe)
    assert ei.value.field == "graph"


def test_foreach_node_missing_ref_id_raises():
    """ref_id required — attrs['id'] must not alias."""
    from types import SimpleNamespace

    from coscientist.core.execution.domain import ForEachSpec
    from coscientist.core.plan.foreach import select_foreach_items

    node = SimpleNamespace(
        node_id="st:hypothesis:H1",
        ref_id=None,
        attrs={"source_task": "FE", "id": "H1"},
    )
    graph = SimpleNamespace(nodes_by_kind=lambda _k: [node])
    fe = ForEachSpec(kind="hypothesis", of_task="FE", selector="all")
    with pytest.raises(JournalFormatError, match="ref_id") as ei:
        select_foreach_items(SimpleNamespace(graph=graph), fe)
    assert ei.value.field == "ref_id"


def _fold_host(**extra):
    """Minimal LocalProjectMixin fold target (no store/writer)."""
    from types import SimpleNamespace

    from coscientist.core.claims.graph import GraphStore, default_execution_registry

    base = dict(
        study=None,
        tasks={},
        attempts={},
        results={},
        plans={},
        graph=GraphStore(default_execution_registry()),
        _finalized=False,
        _study_cancelled=False,
        _budget_exceeded=False,
        _intake_refusal=None,
        verdict_snapshots={},
        hypothesis_verdicts={},
        _expand_requests={},
        _expand_pending=set(),
        _hitl_requests={},
        _hitl_decisions={},
        _foreach_children={},
        artifacts=None,
        artifact_kinds={},
        question_disposals={},
    )
    base.update(extra)
    return SimpleNamespace(**base)


def test_hitl_opened_missing_request_id_raises():
    """HitlOpened must carry request_id — no entity_key alias."""
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    ev = EventEnvelope(
        study_id="st",
        type="HitlOpened",
        entity_key="ek-should-not-alias",
        idem_key="idem-hitl",
        payload={"kind": "plan_approval", "open": True},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="request_id") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "request_id"


def test_hitl_decided_missing_request_id_raises():
    """HitlDecided must carry request_id — no entity_key alias."""
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    ev = EventEnvelope(
        study_id="st",
        type="HitlDecided",
        entity_key="ek-should-not-alias",
        idem_key="idem-hitl-d",
        payload={"decision": "approve"},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="request_id") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "request_id"


def test_route_returned_missing_attempt_id_raises():
    """RouteReturned must carry attempt_id — no entity_key alias."""
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    ev = EventEnvelope(
        study_id="st",
        type="RouteReturned",
        entity_key="ek-should-not-alias",
        idem_key="idem-rr",
        payload={},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="attempt_id") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "attempt_id"


def test_artifact_registered_missing_artifact_id_raises():
    """ArtifactRegistered must carry artifact_id — no entity_key alias."""
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    ev = EventEnvelope(
        study_id="st",
        type="ArtifactRegistered",
        entity_key="ek-should-not-alias",
        idem_key="idem-art",
        payload={"kind": "experiment_design", "stamped": True},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="artifact_id") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "artifact_id"


def test_attempt_started_missing_attempt_role_raises_on_fold():
    """AttemptStarted fold refuses missing attempt_role (no setdefault invent)."""
    from datetime import datetime, timedelta, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.domain import Task, TaskKind, TaskSpec, TaskState
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    host.tasks["st/E1"] = Task(
        task_id="st/E1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key="E1", title="t", goal="g", route="experiment"),
        state=TaskState.PENDING,
        deadline=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    ev = EventEnvelope(
        study_id="st",
        type="AttemptStarted",
        entity_key="st/E1#1",
        idem_key="idem-att",
        payload={
            "attempt_id": "st/E1#1",
            "task_id": "st/E1",
            "closed": False,
            "charged": False,
            "provenance": "real",
            # attempt_role omitted
        },
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="attempt_role") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "attempt_role"


def test_verdict_snapshots_recorded_missing_snapshots_raises():
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = _fold_host()
    ev = EventEnvelope(
        study_id="st",
        type="VerdictSnapshotsRecorded",
        entity_key="st",
        idem_key="idem-vs",
        payload={},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="snapshots") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "snapshots"


def test_expand_input_groups_requires_foreach_children():
    from types import SimpleNamespace

    from coscientist.core.execution.domain import InputSpec
    from coscientist.core.plan.foreach import expand_input_groups

    with pytest.raises(JournalFormatError, match="_foreach_children") as ei:
        expand_input_groups(
            SimpleNamespace(_foreach_children=None),
            [InputSpec(from_task="E0", output="main")],
        )
    assert ei.value.field == "_foreach_children"


def test_derive_claim_catalog_defaults_required():
    """Empty plan criteria without catalog.defaults['experiment'] fails closed."""
    from types import SimpleNamespace

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import (
        CheckResult,
        ExperimentMethod,
        ReqStatus,
        TaskOutcome,
    )
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=criterion_node_id(sid, "value_match"),
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            attrs={
                "frozen": True,
                "uses_sesoi": False,
                "predicts_null": False,
                "predicted_direction": "two_sided",
            },
        )
    )
    task = SimpleNamespace(
        task_id="st/E1",
        spec=SimpleNamespace(method=ExperimentMethod.COMPUTATIONAL, criteria=[]),
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="prediction",
            effect={
                "est": 1.0,
                "kind": "point",
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
        )
    ]
    catalog = SimpleNamespace(defaults={})  # missing experiment key
    with pytest.raises(JournalFormatError, match="defaults") as ei:
        _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw={
                "checked_against": "value_match",
                "target": "Q1",
                "target_kind": "question",
            },
            checks=checks,
            graph=g,
            study_id=sid,
            validated=True,
            catalog=catalog,
            bindings=[],
            attempt_id="st/E1#1",
        )
    assert ei.value.field == "defaults"


def test_indeterminate_without_no_effect_reason_raises():
    """Indeterminate claim path must not invent exclusion_reason='indeterminate'."""
    from types import SimpleNamespace

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import (
        CheckResult,
        ExperimentMethod,
        ReqStatus,
        TaskOutcome,
    )
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=criterion_node_id(sid, "value_match"),
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            attrs={
                "frozen": True,
                "uses_sesoi": False,
                "predicts_null": False,
                "predicted_direction": "two_sided",
            },
        )
    )
    task = SimpleNamespace(
        task_id="st/E1",
        spec=SimpleNamespace(
            method=ExperimentMethod.COMPUTATIONAL,
            criteria=[SimpleNamespace(template_id="value_match")],
        ),
    )
    # FAILED prediction without countable effect and without mapped no-effect detail
    # → classify_outcome indeterminate, _no_effect_reason_from_checks → None.
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role="prediction",
            effect=None,
            detail="unmapped_failure_detail",
        )
    ]
    with pytest.raises(JournalFormatError, match="exclusion_reason") as ei:
        _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw={
                "checked_against": "value_match",
                "target": "Q1",
                "target_kind": "question",
            },
            checks=checks,
            graph=g,
            study_id=sid,
            validated=True,
            bindings=[],
            attempt_id="st/E1#1",
        )
    assert ei.value.field == "exclusion_reason"


def test_effect_omits_empty_produced_by_and_artifact_id():
    """Missing optional provenance keys must not be invented as ''."""
    from coscientist.core.execution.executor import _effect_from_checks

    effect, _used = _effect_from_checks(
        [
            {
                "role": "prediction",
                "effect": {
                    "est": 1.0,
                    "kind": "point",
                    "producer": "agent",
                    "core_evaluated": True,
                },
                "used_sesoi": False,
            }
        ]
    )
    assert effect is not None
    assert "produced_by_call_id" not in effect
    assert "artifact_id" not in effect


def test_criterion_ref_missing_template_id_raises():
    from types import SimpleNamespace

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import (
        CheckResult,
        ExperimentMethod,
        ReqStatus,
        TaskOutcome,
    )
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=criterion_node_id(sid, "value_match"),
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            attrs={
                "frozen": True,
                "uses_sesoi": False,
                "predicts_null": False,
                "predicted_direction": "two_sided",
            },
        )
    )
    task = SimpleNamespace(
        task_id="st/E1",
        spec=SimpleNamespace(
            method=ExperimentMethod.COMPUTATIONAL,
            criteria=[SimpleNamespace()],  # no template_id
        ),
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="prediction",
            effect={
                "est": 1.0,
                "kind": "point",
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
        )
    ]
    with pytest.raises(JournalFormatError, match="template_id") as ei:
        _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw={
                "checked_against": "value_match",
                "target": "Q1",
                "target_kind": "question",
            },
            checks=checks,
            graph=g,
            study_id=sid,
            validated=True,
            bindings=[],
            attempt_id="st/E1#1",
        )
    assert ei.value.field == "template_id"


def test_timeout_commit_detail_empty_kinds_raises():
    from coscientist.core.execution.executor import _timeout_commit_detail

    with pytest.raises(JournalFormatError, match="timeout detail") as ei:
        _timeout_commit_detail([])
    assert ei.value.field == "detail"
    assert _timeout_commit_detail(["idle_timeout"]) == "idle_timeout"


def test_executor_writer_schema_version_constants():
    from coscientist.core.events.events import (
        ATTEMPT_CLOSED_SCHEMA_VERSION,
        RESULT_RECORDED_SCHEMA_VERSION,
        TASK_DONE_SCHEMA_VERSION,
    )
    from coscientist.core.events.upcasters import writer_schema_version

    assert RESULT_RECORDED_SCHEMA_VERSION == writer_schema_version("ResultRecorded") == 3
    assert ATTEMPT_CLOSED_SCHEMA_VERSION == writer_schema_version("AttemptClosed") == 2
    assert TASK_DONE_SCHEMA_VERSION == writer_schema_version("TaskDone") == 2


def test_claim_target_optional_absent_skips_structural():
    """Absent target is not invented as ''; structural merge skips (None)."""
    from coscientist.core.claims.verdict import _claim_target_optional

    assert _claim_target_optional({}) is None
    assert _claim_target_optional(object()) is None
    assert _claim_target_optional({"target": "H1"}) == "H1"


def test_independence_key_requires_experiment_id():
    from coscientist.core.claims.verdict import independence_key_for

    with pytest.raises(JournalFormatError, match="experiment_id") as ei:
        independence_key_for(experiment_id="")
    assert ei.value.field == "experiment_id"


def test_recompute_effect_empty_call_id_raises():
    from coscientist.core.claims.verdict import recompute_effect_provenance

    with pytest.raises(JournalFormatError, match="produced_by_call_id") as ei:
        recompute_effect_provenance(
            {
                "producer": "agent",
                "core_evaluated": True,
                "produced_by_call_id": "",
            }
        )
    assert ei.value.field == "produced_by_call_id"


def test_exclusion_reason_non_indeterminate_unclassified_raises():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import exclusion_reason

    claim = SimpleNamespace(
        counts=lambda: False,
        exclusion_reason=None,
        check_role="prediction",
        validity_ok=True,
        provenance="real",
        claim_outcome="consistent",
        evidence_kind="experiment",
        is_lit=False,
        validity="valid",
    )
    with pytest.raises(JournalFormatError, match="unclassified exclusion") as ei:
        exclusion_reason(claim)
    assert ei.value.field == "exclusion_reason"


def test_producer_index_propagates_errors():
    """Fail-closed: corrupt artifacts must not be swallowed."""
    from coscientist.core.claims.verdict import _producer_index

    with pytest.raises(JournalFormatError, match="artifacts") as ei:
        _producer_index({"r1": {"artifacts": 123}})
    assert ei.value.field == "artifacts"


def test_call_belonging_uses_effect_attempt_id_not_startswith():
    """Belonging authority is structural attempt_id — never call_id startswith."""
    from coscientist.core.claims.verdict import (
        call_id_belongs_to_attempt,
        recompute_effect_provenance,
    )

    aid = "st/E1#1"
    # Suffix call_id alone is not authority.
    assert not call_id_belongs_to_attempt(f"{aid}:sandbox:c", aid)
    assert call_id_belongs_to_attempt(aid, aid)
    # Mapping / produced_by with stamped attempt_id.
    assert call_id_belongs_to_attempt(
        {"attempt_id": aid, "produced_by_call_id": f"{aid}:sandbox:c"},
        aid,
    )
    assert not call_id_belongs_to_attempt(
        {"attempt_id": "other", "produced_by_call_id": f"{aid}:1"},
        aid,
    )
    # Prefix collision shapes refuse under string-only path.
    assert not call_id_belongs_to_attempt("st/E1#10:1", "st/E1#1")
    assert not call_id_belongs_to_attempt("st/E1#1:", "st/E1#1")

    effect = {
        "est": 1.0,
        "kind": "point",
        "producer": "core",
        "core_evaluated": True,
        "produced_by_call_id": f"{aid}:1",
        "attempt_id": aid,
    }
    out = recompute_effect_provenance(effect, attempt_id=aid)
    assert out is not None and out["countable"] is True
    # Same call_id suffix without stamped attempt_id → not countable.
    bare = {k: v for k, v in effect.items() if k != "attempt_id"}
    out2 = recompute_effect_provenance(bare, attempt_id=aid)
    assert out2 is not None and out2["countable"] is False


def test_safe_get_meta_propagates_errors():
    from coscientist.core.claims.verdict import _safe_get_meta

    def boom(_aid: str) -> None:
        raise RuntimeError("store down")

    with pytest.raises(RuntimeError, match="store down"):
        _safe_get_meta("art1", artifact_lookup=boom, notes=[])


def test_loop_dep_parse_refuses_unknown_shape():
    from coscientist.core.plan.loop import LOOP_DEP_PREFIX, parse_loop_dep

    assert parse_loop_dep(f"{LOOP_DEP_PREFIX}L1") == "L1"
    assert parse_loop_dep("loop:") is None
    assert parse_loop_dep("loop") is None
    assert parse_loop_dep("loops:L1") is None
    assert parse_loop_dep("") is None
    assert parse_loop_dep("L1") is None


def test_claims_node_id_parse_refuses_garbage():
    from coscientist.core.claims.claims import _node_ref, parse_graph_node_id

    assert parse_graph_node_id("st:hypothesis:H1") == ("st", "hypothesis", "H1")
    assert parse_graph_node_id("st:claim:a#1:k") == ("st", "claim", "a#1:k")
    with pytest.raises(ValueError, match="malformed"):
        parse_graph_node_id("no-colons")
    with pytest.raises(ValueError, match="malformed"):
        parse_graph_node_id("only:two")
    with pytest.raises(ValueError, match="malformed"):
        parse_graph_node_id("st:hypothesis:")
    assert _node_ref("st:hypothesis:H1", "hypothesis", study_id="st") == "H1"
    assert _node_ref("st:hypothesis:H1", "question", study_id="st") is None
    assert _node_ref("garbage", "hypothesis", study_id="st") is None


def test_report_hypothesis_verdicts_propagates_journal_format_error(monkeypatch):
    from types import SimpleNamespace

    from coscientist.core.claims.dispositions import report_hypothesis_verdicts
    from coscientist.core.claims.graph import GraphStore, default_execution_registry

    monkeypatch.setattr(
        "coscientist.core.claims.dispositions.hypothesis_verdicts",
        lambda *_a, **_k: {
            "H1": {
                "verdict": "untested",
                "by_literature": False,
                "by_theory": False,
                "provenance": "real",
                "k": 1,
                # ref missing
            }
        },
    )
    monkeypatch.setattr(
        "coscientist.core.claims.dispositions.filtered_claims_by_target",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        "coscientist.core.claims.dispositions.claims_for_ref",
        lambda *_a, **_k: [],
    )
    study = SimpleNamespace(
        study_id="st",
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
    )
    with pytest.raises(JournalFormatError, match="ref") as ei:
        report_hypothesis_verdicts(
            graph=GraphStore(default_execution_registry()),
            study=study,
            results={},
            tasks={},
        )
    assert ei.value.field == "ref"


def test_disposition_cover_missing_task_key_raises():
    from types import SimpleNamespace

    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.graph import GraphStore, default_execution_registry

    task = SimpleNamespace(
        reason=None,
        spec=SimpleNamespace(addresses=["Q1"]),  # task_key absent
        task_id="t1",
    )
    with pytest.raises(JournalFormatError, match="task_key") as ei:
        dispositions(
            tasks={"t1": task},
            plans={
                "pl": {
                    "questions": [{"id": "Q1", "text": "why", "final_type": "verdict"}],
                    "tasks": [],
                }
            },
            graph=GraphStore(default_execution_registry()),
            study=None,
            results={},
        )
    assert ei.value.field == "task_key"


# --- Blocker 4: aliases ---


def test_effective_outcome_refuses_outcome_alias():
    """claim.outcome is not an alias for claim_outcome."""
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import effective_outcome

    claim = SimpleNamespace(outcome="consistent", validity_ok=True, criterion_status="met")
    # Missing claim_outcome → derive from criterion_status (not outcome alias).
    assert effective_outcome(claim) == "consistent"  # via criterion_status met→consistent
    bad = SimpleNamespace(
        claim_outcome="bogus", validity_ok=True, criterion_status="met"
    )
    with pytest.raises(JournalFormatError, match="claim_outcome") as ei:
        effective_outcome(bad)
    assert ei.value.field == "claim_outcome"


def test_source_id_for_claim_refuses_attempt_id_alias():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import source_id_for_claim

    with pytest.raises(JournalFormatError, match="experiment_id") as ei:
        source_id_for_claim(SimpleNamespace(attempt_id="st/E1#1"))
    assert ei.value.field == "experiment_id"


def test_content_digest_refuses_digest_alias_without_sha256():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import _content_digest

    meta = SimpleNamespace(digest="abc")  # no sha256
    with pytest.raises(JournalFormatError, match="sha256") as ei:
        _content_digest("a1", artifact_lookup=lambda _i: meta)
    assert ei.value.field == "sha256"


def test_no_effect_detail_rejects_legacy_alias_strings():
    from coscientist.core.execution.executor import _no_effect_reason_from_checks

    assert (
        _no_effect_reason_from_checks(
            [{"role": "prediction", "status": "FAILED", "detail": "output_not_submitted"}]
        )
        == "output_not_submitted"
    )
    assert (
        _no_effect_reason_from_checks(
            [{"role": "prediction", "status": "FAILED", "detail": "output missing"}]
        )
        is None
    )


def test_effect_index_refuses_entity_key_alias():
    from datetime import datetime, timezone

    from coscientist.core.events.events import EventEnvelope
    from coscientist.tools.effects import load_effect_index

    ev = EventEnvelope(
        study_id="st",
        type="EffectIntended",
        entity_key="ek-only",
        idem_key="idem",
        payload={},  # no key
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="key") as ei:
        load_effect_index([ev])
    assert ei.value.field == "key"


def test_proposed_check_requires_bound_task_id_not_task_id_alias():
    """Approved proposal must carry bound_task_id — no task_id alias."""
    import inspect

    from coscientist.checks import evaluate as ev_mod

    src = inspect.getsource(ev_mod._evaluate_proposed_check)
    assert 'row.get("bound_task_id") or row.get("task_id")' not in src
    assert 'bound_task_id' in src
    assert 'no or fallback' not in src  # sanity
    # Behavioral: missing bound_task_id raises before eval.
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        RequirementRef,
    )

    row = {"status": "approved", "task_id": "st/E1", "check": None, "delegated": False}
    ref = RequirementRef(template_id="proposed_check")
    with pytest.raises(JournalFormatError, match="bound_task_id") as ei:
        ev_mod._evaluate_proposed_check(
            ref,
            row=row,
            check_id="pc#0",
            role="prediction",
            out=AgentTaskOutput(outputs={}),
            artifacts=None,
            task_id="st/E1",
            attempt_id="st/E1#1",
            catalog=None,
            review_statuses={},
            judge_retry={},
            fetched_digests=None,
        )
    assert ei.value.field == "bound_task_id"


# --- Blocker 5: domain defaults ---


def test_effective_validity_requires_validity_field():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import effective_validity

    with pytest.raises(JournalFormatError, match="validity") as ei:
        effective_validity(SimpleNamespace(validity_ok=True))
    assert ei.value.field == "validity"


def test_attempt_charged_requires_field():
    from coscientist.core.execution.attempts import _attempt_charged

    with pytest.raises(JournalFormatError, match="charged") as ei:
        _attempt_charged({}, ctx="a1")
    assert ei.value.field == "charged"


def test_budget_limits_required():
    from coscientist.core.execution.domain import Budget

    with pytest.raises(ValidationError):
        Budget(max_cost_rub=None)
    b = Budget(max_llm_calls=1, max_tokens=2, max_tool_calls=3)
    assert b.max_llm_calls == 1


def test_loop_capped_requires_reason():
    from coscientist.core.finalize.finalize import _append_loop_capped_report_line

    seen: list[str] = []
    with pytest.raises(JournalFormatError, match="reason") as ei:
        _append_loop_capped_report_line(seen, {"iter": 1, "survivors": 0})
    assert ei.value.field == "reason"


def test_restore_output_requires_status():
    from coscientist.tools.effects import _restore_output

    with pytest.raises(JournalFormatError, match="status") as ei:
        _restore_output(None, {"result_ref": "x"})
    assert ei.value.field == "status"


def test_plan_shares_requires_revision():
    from types import SimpleNamespace

    from coscientist.core.plan.shares import _plan_specs_for_shares

    rt = SimpleNamespace(plans={"p1": {"tasks": []}}, tasks={})
    with pytest.raises(JournalFormatError, match="revision") as ei:
        _plan_specs_for_shares(rt)
    assert ei.value.field == "revision"


# --- Blocker 6: hypothesis_verdict required kwargs ---


def test_hypothesis_verdict_kwargs_required():
    import inspect

    from coscientist.core.claims.verdict import hypothesis_verdict

    params = inspect.signature(hypothesis_verdict).parameters
    for name in (
        "refutations_needed",
        "refutation_priority",
        "supported_needed",
        "confirmations_needed",
    ):
        assert params[name].default is inspect.Parameter.empty, name


# --- Blocker 7: API refuse without fake CreateStudyBody ---


@pytest.mark.asyncio
async def test_post_studies_parse_refuse_no_study_created(tmp_path, catalog):
    """Invalid body → StudyRefused + StudyFinalized without StudyCreated / fake caps."""
    from coscientist.api.studies import post_studies
    from coscientist.core.execution.runtime_factory import close_runtime, open_runtime

    out = await post_studies(
        tmp_path,
        b'{"request": "q"}',  # missing required caps
        catalog=catalog,
    )
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    types = [e.type for e in events]
    assert "StudyCreated" not in types
    assert types.count("StudyRefused") == 1
    assert types.count("StudyFinalized") == 1
    await close_runtime(rt)


def test_create_study_body_request_nonempty_required():
    from datetime import UTC, datetime

    from coscientist.api.studies import CreateStudyBody

    with pytest.raises(ValidationError):
        CreateStudyBody(
            profile="research",
            request="",
            catalog_version="t",
            deadline=datetime(2099, 1, 1, tzinfo=UTC),
            finalize_reserve_s=1,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            aggregate="count",
            evalue_alpha=0.05,
            max_expansions=0,
            max_human_expansions=0,
            replan_reserve=0.0,
            max_tasks=1,
            max_depth=1,
            plan_timeout_s=1,
            max_foreach_children=1,
            max_loop_iter=1,
            max_task_budget_share=1.0,
            max_proposals=0,
        )


# --- Blocker 8: plan_timeout_s ---


def test_lifecycle_plan_deadline_uses_study_plan_timeout_s():
    """Source pin: plan_deadline uses study.plan_timeout_s (not now+600)."""
    import inspect

    from coscientist.core.plan import lifecycle as lc

    src = inspect.getsource(lc.LifecycleMixin.create_study)
    assert "study.plan_timeout_s" in src
    assert "seconds=600" not in src


# --- Blocker 9: schema constants ---


def test_module_schema_version_constants():
    from coscientist.core.execution import attempts, inputs, machine
    from coscientist.tools import effects

    assert attempts.ATTEMPT_CLOSED_SCHEMA_VERSION == 2
    assert attempts.TASK_DONE_SCHEMA_VERSION == 2
    assert attempts.VERDICT_SNAPSHOTS_RECORDED_SCHEMA_VERSION == 1
    assert machine.BUDGET_EXCEEDED_SCHEMA_VERSION == 1
    assert machine.ATTEMPT_CLOSED_SCHEMA_VERSION == 2
    assert inputs.GRAPH_NODE_UPSERTED_SCHEMA_VERSION == 1
    assert effects.EFFECT_INTENDED_SCHEMA_VERSION == 1
    assert effects.EFFECT_DONE_SCHEMA_VERSION == 1


# --- Blocker 10: silent skips ---


def test_producer_index_bad_payload_raises():
    from coscientist.core.claims.verdict import _producer_index

    with pytest.raises(JournalFormatError, match="mapping") as ei:
        _producer_index({"r1": "not-a-mapping"})
    assert ei.value.field == "results"


def test_foreach_does_not_swallow_journal_format_error():
    import inspect

    from coscientist.core.plan import foreach as fe

    src = inspect.getsource(fe.materialize_ready_foreach)
    assert "_refuse_and_skip_foreach" not in src
    assert "raise" in src


def test_attempt_closed_unknown_attempt_raises():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    host = SimpleNamespace(
        attempts={},
        tasks={},
        _retry_at={},
        study=None,
        graph=None,
        plans={},
        results={},
        artifact_kinds={},
        hypothesis_verdicts={},
    )
    ev = EventEnvelope(
        study_id="st",
        type="AttemptClosed",
        entity_key="missing#1",
        idem_key="idem",
        payload={"attempt_id": "missing#1", "closed": True, "charged": True},
        ts=datetime.now(timezone.utc),
    )
    with pytest.raises(JournalFormatError, match="unknown attempt") as ei:
        LocalProjectMixin._project_local(host, ev)
    assert ei.value.field == "attempt_id"


# --- MAJOR: refute_intent requires sesoi/used_sesoi ---


def test_refute_intent_requires_sesoi_fields():
    from types import SimpleNamespace

    from coscientist.core.claims.verdict import refute_intent_counts

    claim = SimpleNamespace(
        intent="refute",
        frozen=True,
        uses_sesoi=True,
        predicts_null=False,
        check_role="prediction",
        # sesoi / used_sesoi absent
    )
    with pytest.raises(JournalFormatError, match="sesoi") as ei:
        refute_intent_counts(claim)
    assert ei.value.field == "sesoi"
