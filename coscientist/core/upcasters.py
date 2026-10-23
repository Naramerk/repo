"""schema_version upcasters: immutable old payloads; read via upcasters[type][v] -> v+1."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coscientist.core.reasons import CloseReason, TaskReason

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# type -> version -> function that upgrades payload from version to version+1
UPCASTERS: dict[str, dict[int, Upcaster]] = {}


def register_upcaster(event_type: str, from_version: int, fn: Upcaster) -> None:
    UPCASTERS.setdefault(event_type, {})[from_version] = fn


def upcast(event_type: str, schema_version: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Apply chained upcasters until no more are registered."""
    version = schema_version
    data = dict(payload)
    while True:
        fn = UPCASTERS.get(event_type, {}).get(version)
        if fn is None:
            break
        data = fn(data)
        version += 1
    return version, data


def _task_created_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("inherited_from", None)
    return out


def _ensure_spec_addresses(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        spec.setdefault("addresses", [])
        out["spec"] = spec
    return out


def _task_created_v3_to_v4_input_mode(payload: dict[str, Any]) -> dict[str, Any]:
    """Pre-PR5 InputSpec.mode default was use_partial; keep that on replay."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        inputs = spec.get("inputs")
        if isinstance(inputs, list):
            rewritten: list[Any] = []
            for item in inputs:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("mode", "use_partial")
                    rewritten.append(item)
                else:
                    rewritten.append(item)
            spec["inputs"] = rewritten
        out["spec"] = spec
    return out


def _task_created_v4_to_v5_input_kind(payload: dict[str, Any]) -> dict[str, Any]:
    """PR7: InputSpec.kind defaults to artifact; ref absent."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        inputs = spec.get("inputs")
        if isinstance(inputs, list):
            rewritten: list[Any] = []
            for item in inputs:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("kind", "artifact")
                    item.setdefault("ref", None)
                    rewritten.append(item)
                else:
                    rewritten.append(item)
            spec["inputs"] = rewritten
        out["spec"] = spec
    return out


def _strip_plan_criterion_roles(criteria: list[Any]) -> list[Any]:
    """Drop plan-side role; catalog template is the sole authority at evaluate time."""
    rewritten: list[Any] = []
    for item in criteria:
        if isinstance(item, dict):
            item = dict(item)
            item.pop("role", None)
            rewritten.append(item)
        else:
            rewritten.append(item)
    return rewritten


def _task_created_v5_to_v6_check_role(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8: strip plan criterion role (resolve from catalog by template_id)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        criteria = spec.get("criteria")
        if isinstance(criteria, list):
            spec["criteria"] = _strip_plan_criterion_roles(criteria)
        out["spec"] = spec
    return out


def _ensure_foreach_quorum_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """PR10: foreach / foreach_item / InputSpec.quorum defaults for old journals."""
    spec = dict(spec)
    spec.setdefault("foreach", None)
    spec.setdefault("foreach_item", None)
    inputs = spec.get("inputs")
    if isinstance(inputs, list):
        rewritten: list[Any] = []
        for item in inputs:
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("quorum", None)
                rewritten.append(item)
            else:
                rewritten.append(item)
        spec["inputs"] = rewritten
    return spec


def _task_created_v6_to_v7_foreach_quorum(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_foreach_quorum_fields(spec)
    return out


def _ensure_share_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """PR12: budget_share / deadline_share default None on old journals."""
    from coscientist.core.shares import ensure_share_defaults

    return ensure_share_defaults(spec)


def _ensure_sandbox_limit_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """PR17: frozen sandbox limits on TaskSpec (aggregate + per-check)."""
    spec = dict(spec)
    spec.setdefault("sandbox_timeout_s", None)
    spec.setdefault("sandbox_memory_mb", None)
    spec.setdefault("sandbox_check_limits", None)
    spec.setdefault("sandbox_limits_note", None)
    return spec


def _task_created_v7_to_v8_sandbox(payload: dict[str, Any]) -> dict[str, Any]:
    """PR17 sandbox limits on TaskCreated v8 (shares are v9)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_sandbox_limit_fields(spec)
    return out


def _task_created_v8_to_v9_shares(payload: dict[str, Any]) -> dict[str, Any]:
    """PR12: budget_share / deadline_share defaults on TaskCreated v9."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_share_fields(spec)
    return out


def _ensure_output_rubric_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """PR13: OutputSpec.rubric default null on TaskCreated / PlanExpanded tasks."""
    out = dict(spec)
    outs = out.get("expected_outputs")
    if isinstance(outs, list):
        new_outs: list[Any] = []
        for o in outs:
            if isinstance(o, dict):
                o = dict(o)
                o.setdefault("rubric", None)
                new_outs.append(o)
            else:
                new_outs.append(o)
        out["expected_outputs"] = new_outs
    return out


def _task_created_v9_to_v10_rubric(payload: dict[str, Any]) -> dict[str, Any]:
    """PR13: OutputSpec.rubric on TaskCreated v10 (stacked after shares v9).

    If PR18 (#20) merges second and also bumps TaskCreated, resolve order in
    upcasters.py so this step remains contiguous.
    """
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_output_rubric_defaults(spec)
    return out


def _plan_expanded_v1_to_v2_rubric(payload: dict[str, Any]) -> dict[str, Any]:
    """PR13: rubric defaults on PlanExpanded.added_tasks (+ questions final_type)."""
    out = dict(payload)
    added = out.get("added_tasks")
    if isinstance(added, list):
        rewritten: list[Any] = []
        for t in added:
            if isinstance(t, dict):
                rewritten.append(_ensure_output_rubric_defaults(dict(t)))
            else:
                rewritten.append(t)
        out["added_tasks"] = rewritten
    questions = out.get("added_questions")
    if isinstance(questions, list):
        rewritten_q: list[Any] = []
        for q in questions:
            if isinstance(q, dict):
                q = dict(q)
                q.setdefault("final_type", "verdict")
                rewritten_q.append(q)
            else:
                rewritten_q.append(q)
        out["added_questions"] = rewritten_q
    return out


def _plan_accepted_v4_to_v5_foreach_quorum(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                rewritten.append(_ensure_foreach_quorum_fields(dict(t)))
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _plan_accepted_v5_to_v6_sandbox(payload: dict[str, Any]) -> dict[str, Any]:
    """PR17 sandbox limits on PlanAccepted v6 (shares are v7)."""
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                rewritten.append(_ensure_sandbox_limit_fields(dict(t)))
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _plan_accepted_v6_to_v7_shares(payload: dict[str, Any]) -> dict[str, Any]:
    """PR12: budget_share / deadline_share defaults on PlanAccepted v7."""
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                rewritten.append(_ensure_share_fields(dict(t)))
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _ensure_rubric_final_type_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """PR13: QuestionSpec.final_type + OutputSpec.rubric defaults."""
    out = dict(payload)
    questions = out.get("questions")
    if isinstance(questions, list):
        rewritten_q: list[Any] = []
        for q in questions:
            if isinstance(q, dict):
                q = dict(q)
                q.setdefault("final_type", "verdict")
                rewritten_q.append(q)
            else:
                rewritten_q.append(q)
        out["questions"] = rewritten_q
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten_t: list[Any] = []
        for t in tasks:
            if not isinstance(t, dict):
                rewritten_t.append(t)
                continue
            t = dict(t)
            outs = t.get("expected_outputs")
            if isinstance(outs, list):
                new_outs: list[Any] = []
                for o in outs:
                    if isinstance(o, dict):
                        o = dict(o)
                        o.setdefault("rubric", None)
                        new_outs.append(o)
                    else:
                        new_outs.append(o)
                t["expected_outputs"] = new_outs
            rewritten_t.append(t)
        out["tasks"] = rewritten_t
    return out


def _plan_accepted_v7_to_v8_rubric(payload: dict[str, Any]) -> dict[str, Any]:
    """PR13: final_type + rubric on PlanAccepted v8."""
    return _ensure_rubric_final_type_fields(payload)


def _attempt_started_v4_to_v5_shares(payload: dict[str, Any]) -> dict[str, Any]:
    """PR12: P2 share allocation fields on AttemptStarted."""
    out = dict(payload)
    out.setdefault("allocated_budget_rub", None)
    out.setdefault("share_renorm", False)
    return out


def _attempt_started_v5_to_v6_budget_limit(payload: dict[str, Any]) -> dict[str, Any]:
    """PR12: remaining task-cap limit for the runner on AttemptStarted."""
    out = dict(payload)
    out.setdefault("budget_limit_rub", None)
    return out


def _plan_accepted_v2_to_v3_check_role(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8: strip PlanAccepted.tasks[].criteria role (catalog is source of truth)."""
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                t = dict(t)
                criteria = t.get("criteria")
                if isinstance(criteria, list):
                    t["criteria"] = _strip_plan_criterion_roles(criteria)
                rewritten.append(t)
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _plan_accepted_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("questions", [])
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                t = dict(t)
                t.setdefault("addresses", [])
                rewritten.append(t)
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _study_created_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Old journals had no finalize_reserve_s; default 0 keeps pre-reserve deadlines."""
    out = dict(payload)
    out.setdefault("finalize_reserve_s", 0)
    return out


def _study_created_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """PR 6: k_ref / refutation_priority defaults for old StudyCreated journals."""
    out = dict(payload)
    out.setdefault("refutations_needed", 1)
    out.setdefault("refutation_priority", False)
    return out


def _study_created_v3_to_v4(payload: dict[str, Any]) -> dict[str, Any]:
    """PR 8: confirmations_needed (k) as its own schema bump (not piggybacked on v3)."""
    out = dict(payload)
    out.setdefault("confirmations_needed", 1)
    return out


def _study_created_v4_to_v5(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b early: aggregate / evalue_alpha (e-value deferred; keep fields for replay)."""
    out = dict(payload)
    out.setdefault("aggregate", "count")
    out.setdefault("evalue_alpha", 0.05)
    # Old journals that stored evalue still replay as count (no softer verdict).
    if out.get("aggregate") == "evalue":
        out["aggregate"] = "count"
    return out


def _study_created_v5_to_v6(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: supported_needed (≥2); confirmations_needed stays PR8 snapshot k."""
    out = dict(payload)
    out.setdefault("supported_needed", 2)
    try:
        if int(out.get("supported_needed", 2)) < 2:
            out["supported_needed"] = 2
    except (TypeError, ValueError):
        out["supported_needed"] = 2
    return out


def _study_created_v6_to_v7(payload: dict[str, Any]) -> dict[str, Any]:
    """PR9: expansion quotas + replan_reserve (max_expansions ← max_replans)."""
    out = dict(payload)
    if "max_expansions" not in out:
        # Migration: max_expansions replaces max_replans when present.
        if "max_replans" in out:
            try:
                out["max_expansions"] = int(out["max_replans"])
            except (TypeError, ValueError):
                out["max_expansions"] = 2
        else:
            out["max_expansions"] = 2
    out.setdefault("max_human_expansions", 10)
    out.setdefault("replan_reserve", 0.1)
    out.setdefault("max_tasks", 64)
    out.setdefault("max_depth", 8)
    out.setdefault("plan_timeout_s", 600)
    return out


def _study_created_v7_to_v8(payload: dict[str, Any]) -> dict[str, Any]:
    """PR10: max_foreach_children default."""
    out = dict(payload)
    out.setdefault("max_foreach_children", 32)
    return out


def _study_created_v8_to_v9(payload: dict[str, Any]) -> dict[str, Any]:
    """PR12: max_task_budget_share default 1.0."""
    out = dict(payload)
    out.setdefault("max_task_budget_share", 1.0)
    return out


def _plan_accepted_v3_to_v4_expand_after(payload: dict[str, Any]) -> dict[str, Any]:
    """PR9: PlanAccepted.expand_after defaults to []."""
    out = dict(payload)
    out.setdefault("expand_after", [])
    return out


def _study_finalized_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: minimal_report.hypotheses[] (empty for legacy)."""
    out = dict(payload)
    mr = out.get("minimal_report")
    if isinstance(mr, dict):
        mr = dict(mr)
        mr.setdefault("hypotheses", [])
        mr.setdefault("hypotheses_unavailable", None)
        out["minimal_report"] = mr
    out.setdefault("hypothesis_report", out.get("hypothesis_report") or [])
    out.setdefault("hypotheses_unavailable", None)
    return out


def _coerce_task_reason_field(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    reason = out.get("reason")
    if reason is not None:
        try:
            out["reason"] = TaskReason(reason).value
        except ValueError as e:
            raise ValueError(f"unknown TaskDone.reason: {reason!r}") from e
    return out


def _coerce_close_reason_field(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    close_reason = out.get("close_reason")
    if close_reason is not None:
        try:
            out["close_reason"] = CloseReason(close_reason).value
        except ValueError as e:
            raise ValueError(f"unknown AttemptClosed.close_reason: {close_reason!r}") from e
    return out


def _rename_simulated_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy provenance/source simulated→stub and verdict simulated→stubbed."""
    out = dict(payload)
    if out.get("provenance") == "simulated":
        out["provenance"] = "stub"
    if out.get("source") == "simulated":
        out["source"] = "stub"
    if out.get("verdict") == "simulated":
        out["verdict"] = "stubbed"
    inputs = out.get("inputs")
    if isinstance(inputs, list):
        rewritten: list[Any] = []
        for item in inputs:
            if isinstance(item, dict) and item.get("source") == "simulated":
                rewritten.append({**item, "source": "stub"})
            else:
                rewritten.append(item)
        out["inputs"] = rewritten
    return out


def _study_finalized_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Add refusal + structured question counts on minimal_report (PR 4)."""
    out = dict(payload)
    out.setdefault("refusal", None)
    mr = out.get("minimal_report")
    if isinstance(mr, dict):
        mr = dict(mr)
        mr.setdefault("refusal", out.get("refusal"))
        qs = mr.get("questions") or []
        if not isinstance(qs, list):
            qs = []
        mr.setdefault("question_count", len(qs))
        if "not_attempted_count" not in mr:
            mr["not_attempted_count"] = sum(
                1 for q in qs if isinstance(q, dict) and q.get("disposition") == "not_attempted"
            )
        out["minimal_report"] = mr
    return out


def _attempt_started_v2_to_v3_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    """PR7: P2 InputBinding list (+ digest) on AttemptStarted; old journals → []."""
    out = dict(payload)
    out.setdefault("inputs", [])
    return out


def _attempt_started_v3_to_v4_limitations(payload: dict[str, Any]) -> dict[str, Any]:
    """PR10: fan-in limitations on AttemptStarted; old journals → []."""
    out = dict(payload)
    out.setdefault("limitations", [])
    return out


def _attempt_started_v6_to_v7_tainted(payload: dict[str, Any]) -> dict[str, Any]:
    """PR18: core-computed tainted_context on AttemptStarted; old → False.

    Stacked after PR12 v5 shares / v6 budget_limit (both PRs bumped AttemptStarted).
    """
    out = dict(payload)
    out.setdefault("tainted_context", False)
    return out


register_upcaster("TaskCreated", 1, _task_created_v1_to_v2)
register_upcaster("TaskCreated", 2, _ensure_spec_addresses)
register_upcaster("TaskCreated", 3, _task_created_v3_to_v4_input_mode)
register_upcaster("TaskCreated", 4, _task_created_v4_to_v5_input_kind)
register_upcaster("TaskCreated", 5, _task_created_v5_to_v6_check_role)
register_upcaster("TaskCreated", 6, _task_created_v6_to_v7_foreach_quorum)
register_upcaster("TaskCreated", 7, _task_created_v7_to_v8_sandbox)
register_upcaster("TaskCreated", 8, _task_created_v8_to_v9_shares)
register_upcaster("TaskCreated", 9, _task_created_v9_to_v10_rubric)
register_upcaster("PlanAccepted", 1, _plan_accepted_v1_to_v2)
register_upcaster("PlanAccepted", 2, _plan_accepted_v2_to_v3_check_role)
register_upcaster("PlanAccepted", 3, _plan_accepted_v3_to_v4_expand_after)
register_upcaster("PlanAccepted", 4, _plan_accepted_v4_to_v5_foreach_quorum)
register_upcaster("PlanAccepted", 5, _plan_accepted_v5_to_v6_sandbox)
register_upcaster("PlanAccepted", 6, _plan_accepted_v6_to_v7_shares)
register_upcaster("PlanAccepted", 7, _plan_accepted_v7_to_v8_rubric)
register_upcaster("PlanExpanded", 1, _plan_expanded_v1_to_v2_rubric)
register_upcaster("StudyCreated", 1, _study_created_v1_to_v2)
register_upcaster("StudyCreated", 2, _study_created_v2_to_v3)
register_upcaster("StudyCreated", 3, _study_created_v3_to_v4)
register_upcaster("StudyCreated", 4, _study_created_v4_to_v5)
register_upcaster("StudyCreated", 5, _study_created_v5_to_v6)
register_upcaster("StudyCreated", 6, _study_created_v6_to_v7)
register_upcaster("StudyCreated", 7, _study_created_v7_to_v8)
register_upcaster("StudyCreated", 8, _study_created_v8_to_v9)
register_upcaster("TaskDone", 1, _coerce_task_reason_field)
register_upcaster("AttemptClosed", 1, _coerce_close_reason_field)
register_upcaster("AttemptStarted", 1, _rename_simulated_provenance)
register_upcaster("AttemptStarted", 2, _attempt_started_v2_to_v3_inputs)
register_upcaster("AttemptStarted", 3, _attempt_started_v3_to_v4_limitations)
register_upcaster("AttemptStarted", 4, _attempt_started_v4_to_v5_shares)
register_upcaster("AttemptStarted", 5, _attempt_started_v5_to_v6_budget_limit)
register_upcaster("AttemptStarted", 6, _attempt_started_v6_to_v7_tainted)


def _result_recorded_v2_to_v3_output_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: ``output_kinds`` (catalog code|data); absent → {}."""
    out = dict(payload)
    out.setdefault("output_kinds", {})
    return out


register_upcaster("ResultRecorded", 1, _rename_simulated_provenance)
register_upcaster("ResultRecorded", 2, _result_recorded_v2_to_v3_output_kinds)
register_upcaster("StudyFinalized", 1, _study_finalized_v1_to_v2)
register_upcaster("StudyFinalized", 2, _study_finalized_v2_to_v3)
