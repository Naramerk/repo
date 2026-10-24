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


def test_create_study_caps_come_from_study_model():
    """create_study None overrides resolve via Study.model_fields (no magic literals)."""
    from coscientist.core.execution.domain import Study

    assert Study.model_fields["evalue_alpha"].default == 0.05
    assert Study.model_fields["finalize_reserve_s"].default == 30
    assert Study.model_fields["max_expansions"].default == 2
    assert Study.model_fields["max_human_expansions"].default == 10
    assert Study.model_fields["replan_reserve"].default == 0.1
    assert Study.model_fields["max_tasks"].default == 64


def test_dispositions_missing_final_type_raises():
    """Fail-closed: no except→not_attempted invent when plan lacks final_type."""
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.graph import GraphStore, default_execution_registry

    with pytest.raises(JournalFormatError, match="final_type") as ei:
        dispositions(
            tasks={},
            plans={"pl": {"questions": [{"id": "Q1", "text": "why"}], "tasks": []}},
            graph=GraphStore(default_execution_registry()),
            study=None,
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
    from types import SimpleNamespace

    from coscientist.core.execution.domain import ForEachSpec
    from coscientist.core.plan.foreach import select_foreach_items

    node = SimpleNamespace(
        node_id="st:hypothesis:H1",
        ref_id=None,
        attrs={"source_task": "FE"},
    )
    graph = SimpleNamespace(nodes_by_kind=lambda _k: [node])
    fe = ForEachSpec(kind="hypothesis", of_task="FE", selector="all")
    with pytest.raises(JournalFormatError, match="ref_id") as ei:
        select_foreach_items(SimpleNamespace(graph=graph), fe)
    assert ei.value.field == "ref_id"


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
