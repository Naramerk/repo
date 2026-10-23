"""PlanExpanded apply path: quotas, replan_reserve, question supersede (M1 PR9).

Trigger lives in ``expand_trigger``; supersede helpers in ``supersede``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.domain import (
    Plan,
    QuestionSpec,
    QuestionSupersede,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_trigger import refuse_expansion
from coscientist.core.graph import GraphEdge, GraphNode
from coscientist.core.ids import plan_id, question_node_id, task_id
from coscientist.core.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.supersede import plan_node_id

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

Origin = Literal["plan", "emergent"]
DelegatedBy = Literal["human", "timeout"] | None


def _active_plan(runtime: StudyRuntime) -> dict[str, Any] | None:
    if not runtime.plans:
        return None
    return max(runtime.plans.values(), key=lambda p: int(p.get("revision") or 0))


def _quota_kind(delegated_by: DelegatedBy) -> Literal["agent", "human"]:
    return "human" if delegated_by == "human" else "agent"


async def expand_plan(
    runtime: StudyRuntime,
    *,
    after_task: str,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec] | None = None,
    superseded_tasks: list[str] | None = None,
    superseded_questions: list[QuestionSupersede] | None = None,
    origin: Origin = "emergent",
    reason: str = "",
    delegated_by: DelegatedBy = None,
    expand_after: list[str] | None = None,
) -> str | None:
    """Apply a plan expansion. Returns new plan_id, or None if ExpansionRefused.

    Human / ``delegated_by=human`` uses ``max_human_expansions`` and does not
    consume the agent ``max_expansions`` quota (DESIGN_M1 §4).
    """
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()
    added_questions = list(added_questions or [])
    superseded_tasks = list(superseded_tasks or [])
    superseded_questions = list(superseded_questions or [])
    expand_after = list(expand_after or [])

    qkind = _quota_kind(delegated_by)
    agent_n = int(getattr(runtime, "_agent_expansions", 0))
    human_n = int(getattr(runtime, "_human_expansions", 0))
    if qkind == "human":
        if human_n >= study.max_human_expansions:
            await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="human_quota")
            return None
    elif agent_n >= study.max_expansions:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="agent_quota")
        return None

    used = float(getattr(runtime, "_replan_used_s", 0.0))
    budget = study.replan_budget_s()
    remaining_budget = budget - used
    ed = study.effective_deadline()
    time_left = max(0.0, (ed - now).total_seconds())
    allocate = min(float(study.plan_timeout_s), remaining_budget, time_left)
    if remaining_budget <= 0 or allocate <= 0:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="replan_reserve")
        return None

    prev = _active_plan(runtime)
    prev_rev = int(prev.get("revision") or 0) if prev else 0
    prev_pid = str(prev.get("plan_id") or "") if prev else ""
    new_rev = prev_rev + 1

    prev_tasks = [
        TaskSpec.model_validate(t) if isinstance(t, dict) else t for t in (prev.get("tasks") if prev else []) or []
    ]
    skip_keys = set(superseded_tasks)
    kept = [t for t in prev_tasks if t.task_key not in skip_keys]
    superseded_old = {s.old_id for s in superseded_questions}
    active_q_after: set[str] = set()
    for q in (prev.get("questions") if prev else []) or []:
        qid = q.get("id") if isinstance(q, dict) else getattr(q, "id", None)
        if qid and qid not in superseded_old:
            active_q_after.add(str(qid))
    for q in added_questions:
        active_q_after.add(q.id)
    still_kept: list[TaskSpec] = []
    for t in kept:
        addrs = set(t.addresses or [])
        tid_exist = task_id(sid, t.task_key)
        rt_task = runtime.tasks.get(tid_exist)
        already_done = rt_task is not None and rt_task.state == TaskState.DONE
        # Only open tasks that solely address superseded questions are dropped;
        # DONE tasks stay in the revision so dependents can keep depends_on.
        if not already_done and addrs and addrs <= superseded_old and not (addrs & active_q_after):
            skip_keys.add(t.task_key)
        else:
            still_kept.append(t)
    superseded_tasks = sorted(skip_keys)

    merged_tasks = still_kept + list(added_tasks)
    if len(merged_tasks) > study.max_tasks:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="max_tasks")
        return None

    runtime._validate_dag(merged_tasks)  # noqa: SLF001
    known_texts = await runtime._known_question_texts()  # noqa: SLF001
    runtime._validate_questions(added_questions, known_texts)  # noqa: SLF001
    known_texts = {**known_texts, **{q.id: q.text for q in added_questions}}
    prev_q_ids = {
        (q.get("id") if isinstance(q, dict) else getattr(q, "id", None))
        for q in (prev.get("questions") if prev else []) or []
    }
    for s in superseded_questions:
        if s.old_id not in known_texts and s.old_id not in prev_q_ids:
            raise ValueError(f"unknown superseded question id: {s.old_id!r}")
        if s.new_id not in {q.id for q in added_questions} and s.new_id not in known_texts:
            raise ValueError(f"supersede new_id not in added_questions: {s.new_id!r}")
    runtime._validate_addresses(merged_tasks, set(known_texts))  # noqa: SLF001
    validate_input_specs(merged_tasks)
    catalog = getattr(runtime, "catalog", None) or getattr(getattr(runtime, "executor", None), "catalog", None)
    validate_plan_criteria(merged_tasks, catalog)

    prev_questions = [
        QuestionSpec.model_validate(q) if isinstance(q, dict) else q
        for q in (prev.get("questions") if prev else []) or []
    ]
    kept_q = [q for q in prev_questions if q.id not in superseded_old]
    all_questions = kept_q + added_questions

    pid = plan_id()
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged_tasks,
        questions=all_questions,
        expand_after=expand_after,
    )
    events: list[EventEnvelope] = []
    events.extend(
        _plan_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            prev_pid=prev_pid,
            new_rev=new_rev,
            prev_rev=prev_rev,
            origin=origin,
            now=now,
        )
    )
    events.extend(
        _question_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            added_questions=added_questions,
            superseded_questions=superseded_questions,
            known_texts=known_texts,
            now=now,
        )
    )
    events.extend(_skip_superseded_task_events(runtime, sid=sid, superseded_tasks=superseded_tasks, now=now))
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added_tasks:
        if spec.task_key in existing_keys and spec.task_key not in skip_keys:
            continue
        tid = task_id(sid, spec.task_key)
        task = Task(
            task_id=tid,
            kind=TaskKind.EXPERIMENT,
            spec=spec,
            state=TaskState.PENDING,
            deadline=ed,
        )
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, pid),
                payload=task.model_dump(mode="json"),
                ts=now,
                schema_version=6,
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
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", pid),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    )
    await runtime._append(events)  # noqa: SLF001
    runtime._replan_used_s = used + allocate  # noqa: SLF001
    return pid


def _plan_graph_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    pid: str,
    prev_pid: str,
    new_rev: int,
    prev_rev: int,
    origin: str,
    now: Any,
) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    pnid = plan_node_id(sid, pid)
    events.append(
        EventEnvelope(
            study_id=sid,
            type="GraphNodeUpserted",
            entity_key=pnid,
            idem_key=make_idem("GraphNodeUpserted", pnid),
            payload=GraphNode(
                node_id=pnid,
                kind="plan",
                study_id=sid,
                ref_id=pid,
                attrs={"revision": new_rev, "origin": origin},
                created_at=now,
            ).model_dump(mode="json"),
            ts=now,
            schema_version=1,
        )
    )
    if not prev_pid:
        return events
    prev_nid = plan_node_id(sid, prev_pid)
    if runtime.graph.get_node(prev_nid) is None:
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=prev_nid,
                idem_key=make_idem("GraphNodeUpserted", prev_nid, "backfill"),
                payload=GraphNode(
                    node_id=prev_nid,
                    kind="plan",
                    study_id=sid,
                    ref_id=prev_pid,
                    attrs={"revision": prev_rev},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    eid = f"supersedes:{pnid}:{prev_nid}"
    events.append(
        EventEnvelope(
            study_id=sid,
            type="GraphEdgeAdded",
            entity_key=eid,
            idem_key=make_idem("GraphEdgeAdded", eid),
            payload=GraphEdge(
                edge_id=eid,
                kind="supersedes",
                study_id=sid,
                src_id=pnid,
                dst_id=prev_nid,
                attrs={},
                created_at=now,
            ).model_dump(mode="json"),
            ts=now,
            schema_version=1,
        )
    )
    return events


def _question_graph_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    pid: str,
    added_questions: list[QuestionSpec],
    superseded_questions: list[QuestionSupersede],
    known_texts: dict[str, str],
    now: Any,
) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    for q in added_questions:
        qnid = question_node_id(sid, q.id)
        if runtime.graph.get_node(qnid) is not None:
            continue
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=qnid,
                idem_key=make_idem("GraphNodeUpserted", qnid, pid),
                payload=GraphNode(
                    node_id=qnid,
                    kind="question",
                    study_id=sid,
                    ref_id=q.id,
                    attrs={"formulation": q.text},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    added_ids = {q.id for q in added_questions}
    for s in superseded_questions:
        new_nid = question_node_id(sid, s.new_id)
        old_nid = question_node_id(sid, s.old_id)
        if runtime.graph.get_node(old_nid) is None:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=old_nid,
                    idem_key=make_idem("GraphNodeUpserted", old_nid, "supersede_old"),
                    payload=GraphNode(
                        node_id=old_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.old_id,
                        attrs={"formulation": known_texts.get(s.old_id, "")},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        if runtime.graph.get_node(new_nid) is None and s.new_id not in added_ids:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=new_nid,
                    idem_key=make_idem("GraphNodeUpserted", new_nid, "supersede_new"),
                    payload=GraphNode(
                        node_id=new_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.new_id,
                        attrs={"formulation": known_texts.get(s.new_id, "")},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        eid = f"supersedes:{new_nid}:{old_nid}"
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphEdgeAdded",
                entity_key=eid,
                idem_key=make_idem("GraphEdgeAdded", eid),
                payload=GraphEdge(
                    edge_id=eid,
                    kind="supersedes",
                    study_id=sid,
                    src_id=new_nid,
                    dst_id=old_nid,
                    attrs={},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    return events


def _skip_superseded_task_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    superseded_tasks: list[str],
    now: Any,
) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    for tkey in superseded_tasks:
        tid = task_id(sid, tkey)
        task = runtime.tasks.get(tid)
        if task is None or task.state == TaskState.DONE:
            continue
        for aid in task.attempts:
            if not runtime.attempts.get(aid, {}).get("closed"):
                events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="AttemptClosed",
                        entity_key=aid,
                        idem_key=make_idem("AttemptClosed", aid, CloseReason.CANCELLED.value),
                        payload={
                            "attempt_id": aid,
                            "closed": True,
                            "close_reason": CloseReason.CANCELLED.value,
                            "task_id": tid,
                        },
                        ts=now,
                        schema_version=2,
                    )
                )
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key=tid,
                idem_key=make_idem("TaskDone", tid, "superseded"),
                payload={
                    "task_id": tid,
                    "outcome": TaskOutcome.SKIPPED.value,
                    "reason": TaskReason.SUPERSEDED.value,
                },
                ts=now,
                schema_version=2,
            )
        )
    return events


# Re-export trigger for callers that import from expand.
from coscientist.core.expand_trigger import maybe_expand_after_task_done  # noqa: E402
from coscientist.core.supersede import (  # noqa: E402
    criterion_is_superseded,
    question_is_superseded,
)

__all__ = [
    "expand_plan",
    "maybe_expand_after_task_done",
    "question_is_superseded",
    "criterion_is_superseded",
]
