"""PlanExpanded apply path: quotas, replan_reserve, question supersede (M1 PR9).

Trigger lives in ``expand_trigger``; validation in ``expand_validate``;
critic gate in ``expand_critic``. Never raise out of tick/record.
``delegated_by`` is derived only from a matching ``HitlDecided`` (MJ-d).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.events import (
    UnstampedEvent,
    make_idem,
)
from coscientist.core.events.ids import plan_id, task_id
from coscientist.core.events.mapping import optional_item
from coscientist.core.events.schema_versions import (
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import (
    ExpansionRequest,
    Plan,
    QuestionSpec,
    QuestionSupersede,
    Task,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.decomposes import VALID_ROLLUP_RULES
from coscientist.core.plan.expand_critic import critic_gate, prepare_request_for_apply
from coscientist.core.plan.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.plan.expand_trigger import MaxDepthExceeded, refuse_expansion
from coscientist.core.plan.expand_validate import (
    active_plan_blob,
    assign_core_keys,
    task_produced_countable_claims,
    validate_expansion,
)
from coscientist.core.plan.loop import DETAIL_MAX_LEN
from coscientist.core.plan.supersede import (
    should_skip_for_question_supersede,
    target_blocks_agent_supersede,
    task_protected_from_agent_skip,
    tasks_needed_by_active_questions,
)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime



def _coerce_model(cls, value):
    if isinstance(value, dict):
        return cls.model_validate(value)
    return value

def task_kind_for_route(route: object):
    from coscientist.core.execution.domain import Route, UnknownRouteError, parse_route

    if isinstance(route, Route):
        raw = route.value
    else:
        raw = route
    try:
        return parse_route(raw).task_kind
    except UnknownRouteError:
        raise


Origin = Literal["plan", "emergent"]
DelegatedBy = Literal["human", "timeout"] | None

def _if_agent(qkind, value):
    if qkind == "agent":
        return value
    return None



def _quota_kind(delegated_by: DelegatedBy) -> Literal["agent", "human"]:
    # DESIGN_M1 §4: only delegated_by=human uses human quota. timeout → agent
    # (and goes through MJ-b refutation checks).
    if delegated_by == "human":
        return "human"
    if any((delegated_by is None, delegated_by == 'timeout')):
        return "agent"
    raise ValueError(f"unknown delegated_by for quota: {delegated_by!r}")

_PLAN_EXPAND_HITL_KINDS = frozenset({"plan", "plan_approval", "plan_expansion"})

def _delegated_by_from_hitl(
    runtime: StudyRuntime,
    hitl_request_id: str,
    *,
    after_task: str,
    plan_key: str | None = None,
) -> DelegatedBy | str:
    """Derive delegated_by from HitlDecided only. Return error detail if missing/reused.

    Plan change → expansion when ``HitlOpened.kind`` is plan / plan_approval /
    plan_expansion **and** ``expand_after_task`` is set and matches this
    ``after_task`` (or PLAN-n key). Kind is not renamed; GateConfig stays on
    the original kind.
    """
    if hitl_request_id in runtime._hitl_expand_used:
        return "hitl_already_used"
    row = optional_item(runtime._hitl_decisions, hitl_request_id)
    if row is None:
        return "missing_hitl_decided"
    req = optional_item(runtime._hitl_requests, hitl_request_id)
    if req is None:
        return "missing_hitl_opened"
    kind = optional_item(req, "kind")
    if any((not isinstance(kind, str), not kind)):
        return "hitl_missing_kind"
    if kind not in _PLAN_EXPAND_HITL_KINDS:
        return f"hitl_wrong_kind:{kind}"
    # Expansion only when expand_after_task is explicitly set (no task_key fallback).
    bound = optional_item(req, "expand_after_task")
    if any((not isinstance(bound, str), not bound)):
        return "hitl_unbound"
    allowed = {after_task}
    if plan_key is not None:
        allowed.add(plan_key)
    if bound not in allowed:
        return f"hitl_wrong_target:{bound}"
    # Sole decision signal: HitlDecided.delegated_by (no approved_by/decided_by aliases).
    # DESIGN_M1 §4: timeout → agent quota path (still expands); only unknown tokens refuse.
    if "delegated_by" not in row:
        return "missing_hitl_decided"
    db = row["delegated_by"]
    if db == "human":
        return "human"
    if db == "timeout":
        return "timeout"
    return "missing_hitl_decided"

async def expand_plan(
    runtime: StudyRuntime,
    *,
    after_task: str,
    added_tasks: list[TaskSpec],
    origin: Origin,
    reason: str,
    assign_keys: bool | None = None,
    added_questions: list[QuestionSpec] | None = None,
    superseded_tasks: list[str] | None = None,
    superseded_questions: list[QuestionSupersede] | None = None,
    hitl_request_id: str | None = None,
    expand_after: list[str] | None = None,
    replan_allocated_s: float | None = None,
    plan_key: str | None = None,
) -> str | None:
    """Apply a plan expansion. Returns new plan_id, or None if ExpansionRefused.

    ``assign_keys`` None means False (keep planner task_keys).
    ``delegated_by`` is not a public argument (MJ-d): pass ``hitl_request_id``
    and it is taken from the matching ``HitlDecided``; unknown id →
    ``ExpansionRefused(invalid_hitl)``.
    """
    if assign_keys is None:
        assign_keys = False
    try:
        return await _expand_plan(
            runtime,
            after_task=after_task,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=superseded_tasks,
            superseded_questions=superseded_questions,
            origin=origin,
            reason=reason,
            hitl_request_id=hitl_request_id,
            expand_after=expand_after,
            assign_keys=assign_keys,
            replan_allocated_s=replan_allocated_s,
            plan_key=plan_key,
        )
    except LeaseLost:
        raise
    except (PlanInvalid, ValidationError, ValueError):
        await refuse_expansion(runtime, after_task, reason="invalid", detail="expand_crash")
        return None  # audit-ok[except_silent_return]: expand.py:181 except_silent_return

async def _expand_plan(
    runtime: StudyRuntime,
    *,
    after_task: str,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec] | None,
    superseded_tasks: list[str] | None,
    superseded_questions: list[QuestionSupersede] | None,
    origin: Origin,
    reason: str,
    hitl_request_id: str | None,
    expand_after: list[str] | None,
    assign_keys: bool,
    replan_allocated_s: float | None,
    plan_key: str | None,
) -> str | None:
    if any((runtime.study is None, runtime._finalized)):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()

    delegated_by: DelegatedBy = None
    if hitl_request_id:
        derived = _delegated_by_from_hitl(
            runtime, hitl_request_id, after_task=after_task, plan_key=plan_key
        )
        if derived == "human":
            delegated_by = "human"
        elif derived == "timeout":
            delegated_by = "timeout"
        else:
            await refuse_expansion(
                runtime,
                after_task,
                reason="invalid_hitl",
                detail=str(derived)[:DETAIL_MAX_LEN],
            )
            return None

    if added_questions is None:
        added_questions = []
    else:
        added_questions = list(added_questions)
    if superseded_tasks is None:
        superseded_tasks = []
    else:
        superseded_tasks = list(superseded_tasks)
    if superseded_questions is None:
        superseded_questions = []
    else:
        superseded_questions = list(superseded_questions)
    if expand_after is None:
        expand_after = []
    else:
        expand_after = list(expand_after)

    if assign_keys:
        keyed = assign_core_keys(runtime, list(added_tasks))
        if isinstance(keyed, str):
            await refuse_expansion(
                runtime, after_task, reason="invalid", detail=keyed[:DETAIL_MAX_LEN]
            )
            return None
        added_tasks = keyed
    else:
        added_tasks = list(added_tasks)

    qkind = _quota_kind(delegated_by)
    agent_n = require_exact_int(runtime._agent_expansions, what="runtime._agent_expansions")
    human_n = require_exact_int(runtime._human_expansions, what="runtime._human_expansions")
    if qkind == "human":
        if human_n >= study.max_human_expansions:
            await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="human_quota")
            return None
    elif agent_n >= study.max_expansions:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="agent_quota")
        return None

    raw_used = runtime._replan_used_s
    if any((type(raw_used) not in (int, float), isinstance(raw_used, bool))):
        raise TypeError(
            f"_replan_used_s must be int|float, got {type(raw_used).__name__}"
        )
    used = (raw_used + 0.0)
    budget = study.replan_budget_s()
    remaining_budget = budget - used
    ed = study.effective_deadline()
    time_left = (ed - now).total_seconds()
    if time_left < 0.0:
        await refuse_expansion(
            runtime, after_task, reason="expansion_limit", detail="past_deadline"
        )
        return None
    if replan_allocated_s is not None:
        if any((type(replan_allocated_s) not in (int, float), isinstance(replan_allocated_s, bool))):
            raise TypeError(
                f"replan_allocated_s must be int|float, got {type(replan_allocated_s).__name__}"
            )
        allocate = (replan_allocated_s + 0.0)
    else:
        plan_to = study.plan_timeout_s
        if any((type(plan_to) not in (int, float), isinstance(plan_to, bool))):
            raise TypeError(
                f"plan_timeout_s must be int|float, got {type(plan_to).__name__}"
            )
        allocate = min((plan_to + 0.0), remaining_budget, time_left)
        if any((remaining_budget <= 0, allocate <= 0)):
            await refuse_expansion(
                runtime, after_task, reason="expansion_limit", detail="replan_reserve"
            )
            return None

    # MJ-b: agent path (incl. HITL timeout) cannot supersede/SKIP past refutation.
    blocked_qs: set[str] = set()
    refute_keys: set[str] = set()
    if qkind == "agent":
        for s in superseded_questions:
            block = target_blocks_agent_supersede(runtime, s.old_id)
            if block:
                await refuse_expansion(
                    runtime, after_task, reason="has_refutation", detail=f"{s.old_id}:{block}"
                )
                return None
            blocked_qs.add(s.old_id)
        for rt in runtime.tasks.values():
            if rt.state != TaskState.DONE and task_protected_from_agent_skip(runtime, rt):
                refute_keys.add(rt.spec.task_key)
                if rt.spec.task_key in superseded_tasks:
                    await refuse_expansion(
                        runtime,
                        after_task,
                        reason="has_refutation",
                        detail=f"{rt.spec.task_key}:open_refute_task",
                    )
                    return None

    prev = active_plan_blob(runtime)
    prev_tasks: list[TaskSpec]
    prev_tasks = []
    known_texts: dict[str, str]
    known_texts = {}
    if prev is None:
        prev_rev = 0
        prev_pid = ""
    else:
        prev_rev = require_exact_int(prev["revision"], what="plan.revision")
        raw_pid = prev["plan_id"]
        if any((not isinstance(raw_pid, str), not raw_pid)):
            await refuse_expansion(
                runtime, after_task, reason="invalid", detail="missing_plan_id"
            )
            return None
        prev_pid = raw_pid
        prev_tasks = [
            _coerce_model(TaskSpec, t)
            for t in prev["tasks"]
        ]
        for q in prev["questions"]:
            if isinstance(q, dict):
                if "id" not in q:
                    await refuse_expansion(
                        runtime, after_task, reason="invalid", detail="plan_question_missing_id"
                    )
                    return None
                qid = q["id"]
                if any((not isinstance(qid, str), not qid)):
                    await refuse_expansion(
                        runtime, after_task, reason="invalid", detail="plan_question_id_not_str"
                    )
                    return None
                if any(('text' not in q, not isinstance(q['text'], str))):
                    await refuse_expansion(
                        runtime,
                        after_task,
                        reason="invalid",
                        detail=f"missing_question_text:{qid}"[:DETAIL_MAX_LEN],
                    )
                    return None
                known_texts[qid] = q["text"]
            else:
                qid = q.id
                if any((not isinstance(qid, str), not qid)):
                    await refuse_expansion(
                        runtime, after_task, reason="invalid", detail="plan_question_id_not_str"
                    )
                    return None
                known_texts[qid] = q.text
    new_rev = prev_rev + 1
    known_texts.update({q.id: q.text for q in added_questions})

    superseded_old = {s.old_id for s in superseded_questions}
    active_q_after: set[str] = {qid for qid in known_texts if qid not in superseded_old}
    for q in added_questions:
        active_q_after.add(q.id)

    try:
        err = validate_expansion(
            runtime,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=superseded_tasks,
            superseded_questions=superseded_questions,
            known_texts=known_texts,
            prev_tasks=prev_tasks,
            active_q_ids=active_q_after,
        )
    except MaxDepthExceeded as e:
        await refuse_expansion(
            runtime,
            after_task,
            reason="expansion_limit",
            detail=str(e)[:DETAIL_MAX_LEN],
        )
        return None  # audit-ok[except_silent_return]: expand.py:397 except_silent_return
    if err is not None:
        if err == "max_tasks":
            reason_code = "expansion_limit"
        else:
            reason_code = "invalid"
        await refuse_expansion(
            runtime, after_task, reason=reason_code, detail=str(err)[:DETAIL_MAX_LEN]
        )
        return None

    skip_keys = {k for k in superseded_tasks if not task_produced_countable_claims(runtime, k)}
    kept = [t for t in prev_tasks if t.task_key not in skip_keys]
    protected = tasks_needed_by_active_questions(
        tasks=kept + list(added_tasks),
        superseded_old=superseded_old,
        active_q_after=active_q_after,
    )
    for t in kept:
        if task_produced_countable_claims(runtime, t.task_key):
            protected.add(t.task_key)

    still_kept: list[TaskSpec]
    still_kept = []
    for t in kept:
        tid_exist = task_id(sid, t.task_key)
        rt_task = optional_item(runtime.tasks, tid_exist)
        already_done = rt_task is not None and rt_task.state == TaskState.DONE
        if already_done:
            still_kept.append(t)
            continue
        if should_skip_for_question_supersede(
            task=t,
            superseded_old=superseded_old,
            active_q_after=active_q_after,
            protected_keys=protected,
            blocked_questions=_if_agent(qkind, blocked_qs),
            refute_keys=_if_agent(qkind, refute_keys),
        ):
            skip_keys.add(t.task_key)
        else:
            still_kept.append(t)
    superseded_tasks = sorted(skip_keys)
    merged_tasks = still_kept + list(added_tasks)

    if prev is not None:
        prev_questions = [
            _coerce_model(QuestionSpec, q)
            for q in prev["questions"]
        ]
    else:
        prev_questions = []
    all_questions = [q for q in prev_questions if q.id not in superseded_old] + added_questions
    # Stamp expansion origin onto new child questions when unset (PR16).
    from coscientist.core.plan.decomposes import stamp_question_origins

    added_ids = {aq.id for aq in added_questions}
    named_added = {aq.id for aq in added_questions if aq.parent_id is not None}
    if origin == "emergent":
        plan_origin = "emergent"
    else:
        plan_origin = None
    stamped_new = stamp_question_origins(
        [q for q in all_questions if q.id in added_ids],
        named_sub_ids=named_added,
        plan_origin=plan_origin,
    )
    by_new = {q.id: q for q in stamped_new}
    all_questions = [by_new.get(q.id, q) for q in all_questions]  # audit-ok[dict_get_default]: e465
    if prev is None:
        await refuse_expansion(
            runtime, after_task, reason="invalid", detail="missing_plan_rollup"
        )
        return None
    if "rollup" not in prev:
        await refuse_expansion(
            runtime, after_task, reason="invalid", detail="missing_plan_rollup"
        )
        return None
    prev_rollup = prev["rollup"]
    if prev_rollup not in VALID_ROLLUP_RULES:
        await refuse_expansion(
            runtime,
            after_task,
            reason="invalid",
            detail=f"invalid_rollup:{prev_rollup!r}"[:DETAIL_MAX_LEN],
        )
        return None
    rollup = prev_rollup
    named_subs = [q for q in all_questions if q.parent_id is not None]

    pid = plan_id()
    from coscientist.core.execution.domain import LoopSpec

    if prev is not None and "loops" in prev:
        prev_loops = [
            _coerce_model(LoopSpec, x)
            for x in prev["loops"]
        ]
    else:
        prev_loops = []
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged_tasks,
        questions=all_questions,
        expand_after=expand_after,
        loops=prev_loops,
        subquestions=named_subs,
        rollup=rollup,  # type: ignore[arg-type]
    )
    events: list[UnstampedEvent]
    events = []
    events.extend(
        plan_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            prev_pid=prev_pid,
            new_rev=new_rev,
            prev_rev=prev_rev,
            origin=origin,
            delegated_by=delegated_by,
            now=now,
        )
    )
    events.extend(
        question_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            added_questions=added_questions,
            superseded_questions=superseded_questions,
            known_texts=known_texts,
            now=now,
        )
    )
    events.extend(skip_superseded_task_events(runtime, sid=sid, superseded_tasks=superseded_tasks, now=now))
    from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits

    catalog = runtime.catalog
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added_tasks:
        if spec.task_key in existing_keys and spec.task_key not in skip_keys:
            continue
        frozen_spec = freeze_task_sandbox_limits(spec, catalog)
        tid = task_id(sid, frozen_spec.task_key)
        kind = task_kind_for_route(frozen_spec.route)
        from coscientist.core.execution.domain import PlanRole

        task = Task(
            task_id=tid,
            kind=kind,
            spec=frozen_spec,
            state=TaskState.PENDING,
            deadline=ed,
            plan_role=PlanRole.NONE,
        )
        events.append(
            UnstampedEvent(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, pid),
                payload=task.model_dump(mode="json"),
                ts=now,
                schema_version=TASK_CREATED_SCHEMA_VERSION,
                )
        )

    payload = {
        **plan.model_dump(mode="json"),
        "after_task": after_task,
        "plan_rev": new_rev,
        "added_tasks": [t.model_dump(mode="json") for t in added_tasks],
        "added_questions": [q.model_dump(mode="json") for q in added_questions],
        "superseded_tasks": superseded_tasks,
        "superseded_questions": [s.model_dump(mode="json") for s in superseded_questions],
        "origin": origin,
        "reason": reason,
        "delegated_by": delegated_by,
        "replan_allocated_s": allocate,
        "prev_plan_id": prev_pid or None,
        "hitl_request_id": hitl_request_id,
        "plan_key": plan_key,
    }
    events.append(
        UnstampedEvent(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", pid),
            payload=payload,
            ts=now,
            schema_version=PLAN_EXPANDED_SCHEMA_VERSION,
                )
    )
    await runtime._append(events)  # noqa: SLF001
    # B1: newly added foreach templates whose of_task is already DONE.
    from coscientist.core.plan.foreach import materialize_ready_foreach

    await materialize_ready_foreach(runtime)
    return pid

def _resolve_plan_n_request(
    runtime: StudyRuntime, plan_key: str, meta: dict
) -> tuple[ExpansionRequest | None, str | None]:
    """Single source of truth: PLAN-n final-attempt ``expansion_request``.

    Uses ``_final_attempt_id`` only (no seed fallback). Attempt/result
    mismatch → ``invalid`` / ``ExpansionRefused``.
    """
    from coscientist.core.events.ids import task_id as make_task_id
    from coscientist.core.plan.expand_trigger import _final_attempt_id, parse_plan_n_expansion

    _ = meta  # seed pending is advisory for the planner only — never applied here
    if runtime.study is None:
        return None, "missing_study"
    sid = runtime.study.study_id
    plan_tid = make_task_id(sid, plan_key)
    if plan_tid not in runtime.tasks:
        return None, "missing_plan_n_task"
    aid = _final_attempt_id(runtime, plan_tid)
    if not aid:
        return None, "missing_planner_attempt"
    # Sole result key is attempt_id (no result: alias dual-lookup).
    row = optional_item(runtime.results, aid)
    if any((not isinstance(row, dict), 'expansion_request' not in row)):
        return None, "missing_planner_output"
    req, err = parse_plan_n_expansion(runtime, plan_tid)
    if req is None:
        if err is None:
            return None, "missing_planner_output"
        return None, err
    return req, None

async def apply_pending_expansion(runtime: StudyRuntime, plan_key: str) -> str | None:
    """Run M1 critic gate, then apply ExpansionRequest after PLAN-n SUCCESS.

    Planner final-attempt ``expansion_request`` is the sole input (DESIGN_M1 §4).
    """
    reqs = runtime._expand_requests
    if not isinstance(reqs, dict):
        await refuse_expansion(
            runtime,
            plan_key,
            reason="invalid",
            detail="missing_expand_requests",
            plan_key=plan_key,
        )
        return None
    meta = optional_item(reqs, plan_key)
    if not isinstance(meta, dict):
        await refuse_expansion(
            runtime,
            plan_key,
            reason="invalid",
            detail="missing_expand_request_meta",
            plan_key=plan_key,
        )
        return None
    if "after_task" not in meta:
        await refuse_expansion(
            runtime,
            plan_key,
            reason="invalid",
            detail="missing_expand_after_task",
            plan_key=plan_key,
        )
        return None
    raw_after = meta["after_task"]
    if any((not isinstance(raw_after, str), not raw_after)):
        await refuse_expansion(
            runtime,
            plan_key,
            reason="invalid",
            detail="invalid_expand_after_task",
            plan_key=plan_key,
        )
        return None
    after = raw_after
    req, err = _resolve_plan_n_request(runtime, plan_key, meta)
    if req is None:
        if err is None:
            detail = "missing_planner_output"
        else:
            detail = err
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail=str(detail)[:DETAIL_MAX_LEN],
            plan_key=plan_key,
        )
        return None
    try:
        reject = critic_gate(runtime, req, after_task=after)
    except MaxDepthExceeded as e:
        await refuse_expansion(
            runtime,
            after,
            reason="expansion_limit",
            detail=str(e)[:DETAIL_MAX_LEN],
            plan_key=plan_key,
        )
        return None  # audit-ok[except_silent_return]: expand.py:704 except_silent_return
    if reject is not None:
        await refuse_expansion(
            runtime,
            after,
            reason="critic_rejected",
            detail=str(reject)[:DETAIL_MAX_LEN],
            plan_key=plan_key,
        )
        return None
    prepared = prepare_request_for_apply(runtime, req)
    if isinstance(prepared, str):
        await refuse_expansion(
            runtime,
            after,
            reason="critic_rejected",
            detail=prepared[:DETAIL_MAX_LEN],
            plan_key=plan_key,
        )
        return None
    # PLAN-n already reserved replan time — do not double-count on PlanExpanded.
    if "replan_allocated_s" not in meta:
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail="missing_replan_allocated_s",
            plan_key=plan_key,
        )
        return None
    raw_already = meta["replan_allocated_s"]
    if any((type(raw_already) not in (int, float), isinstance(raw_already, bool))):
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail="invalid_replan_allocated_s",
            plan_key=plan_key,
        )
        return None
    already = (raw_already + 0.0)
    if already > 0:
        # PLAN-n already reserved replan time — do not double-count on PlanExpanded.
        replan_s = 0.0
    else:
        replan_s = None
    pid = await expand_plan(
        runtime,
        after_task=after,
        added_tasks=list(prepared.tasks),
        added_questions=list(prepared.questions),
        superseded_tasks=list(prepared.superseded_tasks),
        superseded_questions=list(prepared.superseded_questions),
        origin="emergent",
        reason=prepared.reason,
        assign_keys=False,  # already assigned in prepare_request_for_apply
        replan_allocated_s=replan_s,
        plan_key=plan_key,
    )
    # Live clear; fold also clears on PlanExpanded / ExpansionRefused replay.
    if isinstance(reqs, dict):
        if plan_key in reqs:
            reqs.pop(plan_key)
    return pid

from coscientist.core.plan.expand_trigger import maybe_expand_after_task_done  # noqa: E402
from coscientist.core.plan.supersede import (  # noqa: E402
    criterion_is_superseded,
    question_is_superseded,
)

__all__ = [
    "apply_pending_expansion",
    "expand_plan",
    "maybe_expand_after_task_done",
    "question_is_superseded",
    "criterion_is_superseded",
]
