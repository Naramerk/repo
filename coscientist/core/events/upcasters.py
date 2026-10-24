"""schema_version upcasters: immutable old payloads; read via upcasters[type][v] -> v+1."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import PLAN_ACCEPTED_NEED_SINCE

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# type -> version -> function that upgrades payload from version to version+1
UPCASTERS: dict[str, dict[int, Upcaster]] = {}


def register_upcaster(event_type: str, from_version: int, fn: Upcaster) -> None:
    """Register ``from_version → from_version+1``. Duplicate (type, from_version) raises."""
    bucket = UPCASTERS.setdefault(event_type, {})
    if from_version in bucket:
        raise ValueError(
            f"duplicate upcaster for {event_type!r} from_version={from_version}"
        )
    bucket[from_version] = fn


def writer_schema_version(event_type: str) -> int:
    """Current writer version = 1 + max registered from_version (or 1 if none)."""
    versions = UPCASTERS.get(event_type) or {}
    if not versions:
        return 1
    return max(versions) + 1


def assert_upcaster_chain_continuous(event_type: str, writer_version: int) -> None:
    """Require upcasters for every v in 1..writer_version-1 (no gaps)."""
    if writer_version < 1:
        raise ValueError(f"{event_type}: writer_version must be >= 1")
    bucket = UPCASTERS.get(event_type) or {}
    missing = [v for v in range(1, writer_version) if v not in bucket]
    if missing:
        raise AssertionError(f"{event_type}: upcaster chain gaps before v{writer_version}: missing {missing}")


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
    # Import share_alloc directly — shares.__getattr__ re-export is Any to mypy
    # once the events package gains schema_gate/store stamp imports.
    from coscientist.core.plan.share_alloc import ensure_share_defaults

    return ensure_share_defaults(spec)


def _ensure_sandbox_limit_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """PR17: frozen sandbox limits on TaskSpec (aggregate + per-check).

    Does **not** invent ``sandbox_check_limits``. Missing / null stays missing /
    null so TaskCreated v14 can refuse ≤v12 journals that never froze limits
    (never a quiet ``{}`` pin).
    """
    spec = dict(spec)
    spec.setdefault("sandbox_timeout_s", None)
    spec.setdefault("sandbox_memory_mb", None)
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


def _ensure_output_kind_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """PR15: OutputSpec.kind default null (experiment_design | contradiction_map)."""
    out = dict(spec)
    outs = out.get("expected_outputs")
    if isinstance(outs, list):
        new_outs: list[Any] = []
        for o in outs:
            if isinstance(o, dict):
                o = dict(o)
                o.setdefault("kind", None)
                new_outs.append(o)
            else:
                new_outs.append(o)
        out["expected_outputs"] = new_outs
    return out


def _ensure_output_evidence_kind_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """PR15 §6.6: OutputSpec.evidence_kind default null (explicit only)."""
    out = dict(spec)
    outs = out.get("expected_outputs")
    if isinstance(outs, list):
        new_outs: list[Any] = []
        for o in outs:
            if isinstance(o, dict):
                o = dict(o)
                o.setdefault("evidence_kind", None)
                new_outs.append(o)
            else:
                new_outs.append(o)
        out["expected_outputs"] = new_outs
    return out


FINAL_TYPE_COERCED_LINE = "не выполнено: неизвестный final_type приведён к verdict (журнал)"


def _coerce_final_type_default(raw: Any) -> tuple[str, str | None]:
    """Pre-v8/v10 journals: missing or unknown final_type → verdict.

    Returns ``(final_type, report_line_or_None)``. Unknown (not missing) yields
    a report line; current-schema unknown values are refused at plan accept.
    """
    if raw in ("verdict", "artifact", "both"):
        return str(raw), None
    if raw is None:
        return "verdict", None
    return "verdict", FINAL_TYPE_COERCED_LINE


def _task_created_v9_to_v10_rubric(payload: dict[str, Any]) -> dict[str, Any]:
    """PR13: OutputSpec.rubric on TaskCreated v10 (after shares v9)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_output_rubric_defaults(spec)
    return out


def _task_created_v11_to_v12_output_kind(payload: dict[str, Any]) -> dict[str, Any]:
    """PR15: OutputSpec.kind default null on TaskCreated v12 (after PR11 loop v11)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_output_kind_defaults(spec)
    return out


def _task_created_v12_to_v13_evidence_kind(payload: dict[str, Any]) -> dict[str, Any]:
    """PR15: OutputSpec.evidence_kind default null on TaskCreated v13.

    Does not invent ``sandbox_check_limits`` — v14 refuses missing / null maps
    so ≤v12 journals without a real freeze fail closed.
    """
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_output_evidence_kind_defaults(spec)
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
                ft, line = _coerce_final_type_default(q.get("final_type"))
                q["final_type"] = ft
                if line:
                    q["final_type_report_line"] = line
                rewritten_q.append(q)
            else:
                rewritten_q.append(q)
        out["added_questions"] = rewritten_q
    return out


def _plan_expanded_v2_to_v3_loop(payload: dict[str, Any]) -> dict[str, Any]:
    """PR11: loop_id / loop_iter / loop_template_key defaults on PlanExpanded.added_tasks."""
    out = dict(payload)
    added = out.get("added_tasks")
    if isinstance(added, list):
        rewritten: list[Any] = []
        for t in added:
            if isinstance(t, dict):
                rewritten.append(_ensure_loop_fields(dict(t)))
            else:
                rewritten.append(t)
        out["added_tasks"] = rewritten
    return out


def _ensure_pr15_output_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """PR15: OutputSpec.kind + evidence_kind defaults (single PA11/PE5 step)."""
    return _ensure_output_evidence_kind_defaults(_ensure_output_kind_defaults(spec))


def _plan_expanded_v4_to_v5_pr15_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    """PR15: kind + evidence_kind on PlanExpanded.added_tasks (after PR16 decomposes v4)."""
    out = dict(payload)
    added = out.get("added_tasks")
    if isinstance(added, list):
        rewritten: list[Any] = []
        for t in added:
            if isinstance(t, dict):
                rewritten.append(_ensure_pr15_output_defaults(dict(t)))
            else:
                rewritten.append(t)
        out["added_tasks"] = rewritten
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
                ft, line = _coerce_final_type_default(q.get("final_type"))
                q["final_type"] = ft
                if line:
                    q["final_type_report_line"] = line
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


def _ensure_decomposes_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """PR16: Plan.rollup / subquestions + QuestionSpec.origin defaults."""
    out = dict(payload)
    out.setdefault("rollup", "all")
    out.setdefault("subquestions", [])
    for bucket in ("questions", "subquestions", "added_questions"):
        rows = out.get(bucket)
        if not isinstance(rows, list):
            continue
        rewritten: list[Any] = []
        for q in rows:
            if isinstance(q, dict):
                q = dict(q)
                q.setdefault("parent_id", None)
                q.setdefault("origin", None)
                rewritten.append(q)
            else:
                rewritten.append(q)
        out[bucket] = rewritten
    return out


def _plan_accepted_v9_to_v10_decomposes(payload: dict[str, Any]) -> dict[str, Any]:
    """PR16: rollup + subquestions on PlanAccepted v10 (after PR11 loops v9)."""
    return _ensure_decomposes_fields(payload)


def _plan_expanded_v3_to_v4_decomposes(payload: dict[str, Any]) -> dict[str, Any]:
    """PR16: rollup/subquestions/origin on PlanExpanded v4 (after PR11 loop v3)."""
    return _ensure_decomposes_fields(payload)


def _plan_accepted_v10_to_v11_pr15_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    """PR15: kind + evidence_kind on PlanAccepted v11 (after PR16 decomposes v10)."""
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                rewritten.append(_ensure_pr15_output_defaults(dict(t)))
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


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


def _study_created_v9_to_v10(payload: dict[str, Any]) -> dict[str, Any]:
    """PR11 (#24): max_loop_iter default 8."""
    out = dict(payload)
    out.setdefault("max_loop_iter", 8)
    return out


def _study_created_v10_to_v11(payload: dict[str, Any]) -> dict[str, Any]:
    """PR19 (#23) bridge: as_of / user_input_ids on StudyCreated **v11**.

    Identical to #23: ``setdefault`` only — never pop ``max_loop_iter`` (real
    LoopSpec cap on main). Journals with ``max_loop_iter=2`` must replay as 2.
    #23 owns writer v11; this bridge upcasts main v10 journals through v11.
    """
    out = dict(payload)
    out.setdefault("as_of", None)
    out.setdefault("user_input_ids", [])
    return out


def _study_created_v11_to_v12(payload: dict[str, Any]) -> dict[str, Any]:
    """PR20: max_proposals (agent CheckProposed quota; default 3)."""
    out = dict(payload)
    out.setdefault("max_proposals", 3)
    return out


def _study_created_v12_to_v13_catalog_version(payload: dict[str, Any]) -> dict[str, Any]:
    """#41: Study.catalog_version is StrictInt — refuse missing / non-int generations."""
    from coscientist.core.events.errors import CatalogError

    out = dict(payload)
    if "catalog_version" not in out or out["catalog_version"] is None:
        raise CatalogError("StudyCreated.v13:catalog_version_required")
    cv = out["catalog_version"]
    if isinstance(cv, bool) or not isinstance(cv, int):
        raise CatalogError(f"StudyCreated.v13:catalog_version_must_be_strict_int_got_{type(cv).__name__}")
    return out


def _study_created_v13_to_v14_require_hypotheses(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """G4: require_hypotheses required; missing/null → typed refuse (DESIGN §2)."""
    from coscientist.core.events.errors import CatalogError

    out = dict(payload)
    if "require_hypotheses" not in out or out["require_hypotheses"] is None:
        raise CatalogError("StudyCreated.v14:require_hypotheses_required")
    if not isinstance(out["require_hypotheses"], bool):
        raise CatalogError(
            f"StudyCreated.v14:require_hypotheses_must_be_bool_got_{type(out['require_hypotheses']).__name__}"
        )
    return out


def _ensure_loop_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """PR11: loop_id / loop_iter / loop_template_key defaults on old TaskSpec."""
    spec = dict(spec)
    spec.setdefault("loop_id", None)
    spec.setdefault("loop_iter", None)
    spec.setdefault("loop_template_key", None)
    return spec


def _task_created_v10_to_v11_loop(payload: dict[str, Any]) -> dict[str, Any]:
    """PR11: loop_id / loop_iter / loop_template_key on TaskCreated v11 (after #21 rubric v10)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = _ensure_loop_fields(spec)
    return out


def _plan_accepted_v8_to_v9_loops(payload: dict[str, Any]) -> dict[str, Any]:
    """PR11: Plan.loops default [] on PlanAccepted v9 (after #21 rubric v8)."""
    out = dict(payload)
    out.setdefault("loops", [])
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                rewritten.append(_ensure_loop_fields(dict(t)))
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _study_finalized_v3_to_v4(payload: dict[str, Any]) -> dict[str, Any]:
    """PR20: custom_checks[] + observations[] on StudyFinalized / minimal_report."""
    out = dict(payload)
    out.setdefault("custom_checks", [])
    out.setdefault("observations", [])
    mr = out.get("minimal_report")
    if isinstance(mr, dict):
        mr = dict(mr)
        mr.setdefault("custom_checks", [])
        mr.setdefault("observations", [])
        out["minimal_report"] = mr
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


def _attempt_started_v7_to_v8_inputs_digest(payload: dict[str, Any]) -> dict[str, Any]:
    """PR19: inputs_digest + InputBinding snapshot defaults.

    Legacy rows get ``digest_version=None`` so drift treats them as unknown
    (formula changed) without forcing provenance=partial.
    """
    out = dict(payload)
    out.setdefault("inputs_digest", None)
    inputs = out.get("inputs")
    if isinstance(inputs, list):
        rewritten: list[Any] = []
        for row in inputs:
            if isinstance(row, dict):
                r = dict(row)
                r.setdefault("digest", None)
                r.setdefault("digest_version", None)
                r.setdefault("retrieved_at", None)
                r.setdefault("source_uri", None)
                rewritten.append(r)
            else:
                rewritten.append(row)
        out["inputs"] = rewritten
    return out


def _attempt_started_v8_to_v9_role(payload: dict[str, Any]) -> dict[str, Any]:
    """PR20: attempt_role default agent (reviewer bind is ReviewScheduled)."""
    out = dict(payload)
    out.setdefault("attempt_role", "agent")
    return out


def _attempt_started_v9_to_v10_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    """R16a / DESIGN_M1 §6.7: AttemptStarted.provenance required at v10.

    Absent / null / unknown → ``CatalogError`` (never invent ``real``).
    Legacy rename ``simulated``→``stub`` stays at v1; writers already send
    an explicit value. Pre-field journals that omit the key refuse on read;
    tests that need a value use a journal that already carries provenance
    as a committed fact (not a temp-copy remap of legacy).
    """
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import parse_provenance

    out = dict(payload)
    raw = out["provenance"] if "provenance" in out else None
    try:
        out["provenance"] = parse_provenance(raw, owner="AttemptStarted.v10")
    except ValueError as e:
        if raw is None:
            raise CatalogError("AttemptStarted.v10:provenance_required") from e
        raise CatalogError(f"AttemptStarted.v10:provenance_unknown:{raw!r}") from e
    return out


# Forever-fields that early journals may omit; v11 marks them AttemptUnrecorded.
# Provenance is required at v10 (R16a) — not in this set. Not filled by earlier
# inventing upcasters (inputs/limitations/role/…).
_ATTEMPT_STARTED_UNRECORDED_FIELDS = (
    "n",
    "dispatch_id",
    "route_returned",
    "closed",
    "study_id",
)
# source_schema_version stamped on Unrecorded (= last version before this bridge).
ATTEMPT_STARTED_UNRECORDED_SINCE = 10


def _attempt_started_v10_to_v11_unrecorded(payload: dict[str, object]) -> dict[str, object]:
    """Carry only journalled forever-fields; absent → AttemptUnrecorded (no invent).

    Also drops legacy ``task_key`` (never a writer AttemptStarted field).
    """
    out: dict[str, object] = dict(payload)
    if "task_key" in out:
        del out["task_key"]
    pad = {
        "kind": "unrecorded",
        "source_schema_version": ATTEMPT_STARTED_UNRECORDED_SINCE,
    }
    for field in _ATTEMPT_STARTED_UNRECORDED_FIELDS:
        if field not in out:
            out[field] = dict(pad)
    return out


def _task_created_v13_to_v14_sandbox_freeze(payload: dict[str, Any]) -> dict[str, Any]:
    """#41: v14 TaskCreated must carry a complete typed sandbox freeze map.

    Nothing is inferred. A v13 journal row is accepted only if it is already
    v14-shaped: ``spec.sandbox_check_limits`` present, a **non-empty** object,
    and every row a valid ``FrozenCheckLimits`` (explicit bool
    ``allow_network`` / ``allow_cgroup_degraded`` and explicit host budgets).
    A missing / null / empty map means the task predates a real freeze and
    cannot be run under the v14 rules → ``CatalogError`` (never a quiet
    pass-through, never defer empty-map failure to ``apply_frozen_limits``).
    Writers at schema 14 may still persist an empty freeze for all-exempt /
    PLAN-n tasks; that path does not run this upcaster.
    """
    from pydantic import ValidationError

    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import FrozenCheckLimits

    out = dict(payload)
    if "spec" not in out:
        raise CatalogError("TaskCreated.v14:spec_required")
    spec = out["spec"]
    if not isinstance(spec, dict):
        raise CatalogError(f"TaskCreated.v14:spec_must_be_object:{type(spec).__name__}")
    if "sandbox_check_limits" not in spec or spec["sandbox_check_limits"] is None:
        raise CatalogError("TaskCreated.v14:sandbox_check_limits_required")
    limits = spec["sandbox_check_limits"]
    if not isinstance(limits, dict):
        raise CatalogError(f"TaskCreated.v14:sandbox_check_limits_must_be_object:{type(limits).__name__}")
    if not limits:
        raise CatalogError("TaskCreated.v14:sandbox_check_limits_empty")
    for tid, row in limits.items():
        if not isinstance(row, dict):
            raise CatalogError(f"TaskCreated.v14:freeze_row_not_object:{tid}")
        for key in ("allow_network", "allow_cgroup_degraded"):
            if key not in row:
                raise CatalogError(f"TaskCreated.v14:missing_security_toggles:{tid}:{key}")
            if not isinstance(row[key], bool):
                raise CatalogError(f"TaskCreated.v14:bad_security_toggle:{tid}:{key}:{type(row[key]).__name__}")
        try:
            FrozenCheckLimits.model_validate(row)
        except ValidationError as e:
            raise CatalogError(f"TaskCreated.v14:freeze_row_invalid:{tid}:{e.error_count()}_errors") from e
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
register_upcaster("TaskCreated", 10, _task_created_v10_to_v11_loop)
register_upcaster("TaskCreated", 11, _task_created_v11_to_v12_output_kind)
register_upcaster("TaskCreated", 12, _task_created_v12_to_v13_evidence_kind)
register_upcaster("TaskCreated", 13, _task_created_v13_to_v14_sandbox_freeze)


def _plan_accepted_v11_to_v12_need(payload: dict[str, Any]) -> dict[str, Any]:
    """G4: absent Need → NeedUnrecorded; present → strict discriminated union."""
    from pydantic import ValidationError

    from coscientist.core.plan.need import (
        PLAN_NEED_KINDS,
        parse_plan_need,
    )

    out = dict(payload)
    pad = {"kind": "unrecorded", "source_schema_version": PLAN_ACCEPTED_NEED_SINCE}
    for field in ("hypotheses", "literature"):
        if field not in out:
            out[field] = dict(pad)
            continue
        blob = out[field]
        if not isinstance(blob, dict):
            raise JournalFormatError(f"PlanAccepted.v12:need_not_mapping:{field}")
        if "kind" not in blob:
            raise JournalFormatError(f"PlanAccepted.v12:need_kind_missing:{field}")
        if blob["kind"] is None:
            raise JournalFormatError(f"PlanAccepted.v12:need_kind_null:{field}")
        if blob["kind"] not in PLAN_NEED_KINDS:
            raise JournalFormatError(f"PlanAccepted.v12:need_kind_unknown:{field}")
        try:
            out[field] = parse_plan_need(blob).model_dump(mode="json")
        except ValidationError as e:
            raise JournalFormatError(f"PlanAccepted.v12:need_invalid:{field}") from e
    return out


register_upcaster("PlanAccepted", 1, _plan_accepted_v1_to_v2)
register_upcaster("PlanAccepted", 2, _plan_accepted_v2_to_v3_check_role)
register_upcaster("PlanAccepted", 3, _plan_accepted_v3_to_v4_expand_after)
register_upcaster("PlanAccepted", 4, _plan_accepted_v4_to_v5_foreach_quorum)
register_upcaster("PlanAccepted", 5, _plan_accepted_v5_to_v6_sandbox)
register_upcaster("PlanAccepted", 6, _plan_accepted_v6_to_v7_shares)
register_upcaster("PlanAccepted", 7, _plan_accepted_v7_to_v8_rubric)
register_upcaster("PlanAccepted", 8, _plan_accepted_v8_to_v9_loops)
register_upcaster("PlanAccepted", 9, _plan_accepted_v9_to_v10_decomposes)
register_upcaster("PlanAccepted", 10, _plan_accepted_v10_to_v11_pr15_kinds)
register_upcaster("PlanAccepted", PLAN_ACCEPTED_NEED_SINCE, _plan_accepted_v11_to_v12_need)
register_upcaster("PlanExpanded", 1, _plan_expanded_v1_to_v2_rubric)
register_upcaster("PlanExpanded", 2, _plan_expanded_v2_to_v3_loop)
register_upcaster("PlanExpanded", 3, _plan_expanded_v3_to_v4_decomposes)
register_upcaster("PlanExpanded", 4, _plan_expanded_v4_to_v5_pr15_kinds)
register_upcaster("StudyCreated", 1, _study_created_v1_to_v2)
register_upcaster("StudyCreated", 2, _study_created_v2_to_v3)
register_upcaster("StudyCreated", 3, _study_created_v3_to_v4)
register_upcaster("StudyCreated", 4, _study_created_v4_to_v5)
register_upcaster("StudyCreated", 5, _study_created_v5_to_v6)
register_upcaster("StudyCreated", 6, _study_created_v6_to_v7)
register_upcaster("StudyCreated", 7, _study_created_v7_to_v8)
register_upcaster("StudyCreated", 8, _study_created_v8_to_v9)
register_upcaster("StudyCreated", 9, _study_created_v9_to_v10)
register_upcaster("StudyCreated", 10, _study_created_v10_to_v11)
register_upcaster("StudyCreated", 11, _study_created_v11_to_v12)
register_upcaster("StudyCreated", 12, _study_created_v12_to_v13_catalog_version)
register_upcaster("StudyCreated", 13, _study_created_v13_to_v14_require_hypotheses)
register_upcaster("TaskDone", 1, _coerce_task_reason_field)
register_upcaster("AttemptClosed", 1, _coerce_close_reason_field)
register_upcaster("AttemptStarted", 1, _rename_simulated_provenance)
register_upcaster("AttemptStarted", 2, _attempt_started_v2_to_v3_inputs)
register_upcaster("AttemptStarted", 3, _attempt_started_v3_to_v4_limitations)
register_upcaster("AttemptStarted", 4, _attempt_started_v4_to_v5_shares)
register_upcaster("AttemptStarted", 5, _attempt_started_v5_to_v6_budget_limit)
register_upcaster("AttemptStarted", 6, _attempt_started_v6_to_v7_tainted)
register_upcaster("AttemptStarted", 7, _attempt_started_v7_to_v8_inputs_digest)
register_upcaster("AttemptStarted", 8, _attempt_started_v8_to_v9_role)
register_upcaster("AttemptStarted", 9, _attempt_started_v9_to_v10_provenance)
register_upcaster("AttemptStarted", 10, _attempt_started_v10_to_v11_unrecorded)


def _result_recorded_v2_to_v3_output_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: ``output_kinds`` (catalog code|data); absent → {}."""
    out = dict(payload)
    out.setdefault("output_kinds", {})
    return out


register_upcaster("ResultRecorded", 1, _rename_simulated_provenance)
register_upcaster("ResultRecorded", 2, _result_recorded_v2_to_v3_output_kinds)


def _study_refused_v1_to_v2_catalog_version(payload: dict[str, Any]) -> dict[str, Any]:
    """#41: v2 requires ``catalog_version`` (nullable).

    v1 payloads had no such field — set ``null`` to record that absence.
    This is the v1 schema fact, not a guessed generation.
    """
    out = dict(payload)
    if "catalog_version" not in out:
        out["catalog_version"] = None
    return out


register_upcaster("StudyFinalized", 1, _study_finalized_v1_to_v2)
register_upcaster("StudyFinalized", 2, _study_finalized_v2_to_v3)
register_upcaster("StudyFinalized", 3, _study_finalized_v3_to_v4)
register_upcaster("StudyRefused", 1, _study_refused_v1_to_v2_catalog_version)
