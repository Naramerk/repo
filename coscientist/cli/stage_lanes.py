"""HITL stage lanes and wired hyp/lit/report gates (split from run)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.schemas import (
    AgentRun,
    AutomaticExecutorIn,
    ExecutorRouteIn,
    HypothesisOut,
    LiteratureOut,
    RegisteredInput,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.control import StudyControl
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import record_hypotheses, record_literature
from coscientist.cli.report_lane import (
    SCIENCE_ARTIFACT_NAME,
    human_gate_feedback,
    put_science_report,
    render_report_docs,
)
from coscientist.cli.study_helpers import _require_attempt, _usage_row
from coscientist.cli.tool_surface import LiveCliToolSurface
from coscientist.core.claims.reasons import CloseReason
from coscientist.core.events.clock import Clock
from coscientist.core.execution.domain import Budget, Task, TaskOutcome, TaskState
from coscientist.llm.client import LlmClient
from coscientist.llm.errors import LlmError
from coscientist.reporting.science import build_science_report_markdown

if TYPE_CHECKING:
    from coscientist.cli.run import CliToolSurface


_ROLE_HYP = "hypothesizer"
_ROLE_LIT = "researcher"
_ROLE_REPORT = "report"

_StageOut = TypeVar("_StageOut")


def _ctx_role_repair(
    adapter: LlmAgentAdapter,
    *,
    role: str,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    output_cls: type[Any],
) -> Callable[[str], Awaitable[Any]]:
    """Repair callback for ctx-driven stage roles (tz_spec / hypothesizer):
    re-runs the SAME role with the human's exact feedback (HITL v2 §5; the
    adapter renders it as the repair user message; same attempt, no burn)."""

    async def _run(feedback: str) -> Any:
        repair_run = await adapter.run(role, ctx, [], role_budget, repair_feedback=feedback)
        usages.append(_usage_row(role, repair_run))
        if repair_run.error is not None:
            raise CliRefuse(f"{role} error: {repair_run.error}", code=4)
        if not isinstance(repair_run.output, output_cls):
            raise CliRefuse(f"{role} must return {output_cls.__name__}", code=4)
        return repair_run.output

    return _run


async def _journal_feedback_rows(rt: StudyRuntime) -> list[dict[str, str]]:
    """Human gate-feedback rows folded from the journal (report comments section)."""
    return human_gate_feedback(await rt.store.read_all())


async def _stage_gate_review(
    rt: StudyRuntime,
    control: StudyControl,
    *,
    kind: str,
    task_key: str,
    attempt_id: str,
    question: str,
    output: _StageOut,
    snapshot: Callable[[_StageOut], dict[str, object]],
    run_repair: Callable[[str], Awaitable[_StageOut]],
    usages: list[dict[str, object]],
    gate_label: str,
) -> tuple[_StageOut, dict[str, object]]:
    """One interactive stage gate with ONE bounded repair round (hitl-map §5).

    ``revise`` OR ``reject``-with-feedback re-drives the SAME stage attempt
    through ``run_repair`` with the human's exact text (the adapter renders it
    as the repair user message); the corrected output opens a fresh gate
    round. A second return, or a bare reject without repair material, cancels
    the study and refuses typed — naming the gate and the human feedback
    (owner law: honest typed refusal, never silent death).
    """
    outcome = await control.review_stage(
        rt, kind=kind, task_key=task_key, attempt_id=attempt_id,
        question=question, snapshot=snapshot(output),
    )
    usages.append({
        "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
        "decision": outcome.decision, "feedback": outcome.feedback,
    })
    repaired = False
    while outcome.decision in ("revise", "reject"):
        feedback = outcome.feedback
        if feedback is None:
            await rt.cancel_study(by=f"human-{kind}-rejected")
            raise CliRefuse(f"{gate_label}: human rejected without repair feedback", code=3)
        if repaired:
            await rt.cancel_study(by=f"human-{kind}-rejected")
            raise CliRefuse(
                f"{gate_label}: human repair rounds exhausted; feedback: {feedback}",
                code=3,
            )
        repaired = True
        usages.append({
            "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
            "note": "one bounded repair round re-drives the stage role with the human feedback",
        })
        try:
            output = await run_repair(feedback)
        except LlmError as exc:
            # The repair role call failed at the LLM transport level inside
            # the SAME stage attempt: close the attempt retryable via the
            # core, then refuse typed with the human feedback preserved in
            # the detail (owner law — honest refusal, never a raw crash; the
            # gate flow must not silently re-ask past a human touchpoint).
            await rt.fail_attempt(attempt_id, retryable=True, close_reason=CloseReason.FAILED)
            usages.append({
                "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
                "attempt_id": attempt_id,
                "retryable_error": f"gate repair llm error: {exc}",
            })
            raise CliRefuse(
                f"{gate_label}: repair llm error with human feedback {feedback!r}: {exc}",
                code=4,
            ) from exc
        outcome = await control.review_stage(
            rt, kind=kind, task_key=task_key, attempt_id=attempt_id,
            question=question, snapshot=snapshot(output),
        )
        usages.append({
            "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
            "decision": outcome.decision, "feedback": outcome.feedback,
        })
    return output, {"kind": kind, "decision": outcome.decision, "feedback": outcome.feedback}


async def _optional_need_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    role: str,
    needed: bool,
    wired_roles: frozenset[str],
    need_label: str,
    tools: CliToolSurface,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    lit_tool_ids: tuple[str, ...],
    control: StudyControl | None = None,
) -> None:
    """Need-on + unwired → refuse; wired → honest run; need-off defers to accept.

    Wired path mirrors the SPEC stage: one start_attempt → one adapter run →
    journal record (``record_hypotheses`` / ``record_literature``) closes the
    DAG task non-SKIPPED. Runtime/agent failures refuse terminal (rc=4) —
    stage retries are not re-driven by the CLI loop (same exposure as SPEC).

    Terminal DAG state is written from the ACCEPTED plan only: need-off here
    does NOT cancel the stage (an intermediate round may flip need back on —
    the wired stage stays startable); the core's accept_plan closes not-needed
    HYP/LIT SKIPPED in the PlanAccepted transaction. A DONE SUCCESS stage
    (satisfied in an earlier round) is never re-driven — the journal record
    stands; a DONE non-SUCCESS stage on a needed=true flip is drift → typed
    refuse (TaskDone is terminal, the core has no reopen).
    """
    if not needed:
        usages.append(
            {
                "role": role,
                "task_key": task_key,
                "note": (
                    f"{need_label} not needed this round; closure defers to the "
                    "accepted plan (need stages stay startable until accept)"
                ),
            }
        )
        return
    stage = _task_by_key(rt, task_key)
    if stage is not None and stage.state == TaskState.DONE:
        if stage.outcome == TaskOutcome.SUCCESS:
            usages.append(
                {
                    "role": role,
                    "task_key": task_key,
                    "note": (
                        f"{need_label} already journaled in an earlier plan round; "
                        "need stages are one-shot — not re-driven"
                    ),
                }
            )
            return
        raise CliRefuse(
            f"planner set {need_label}.needed=true but the {task_key} stage closed as "
            f"{str(stage.outcome)} in an earlier plan round (need stages are one-shot; "
            "TaskDone is terminal — no reopen)",
            code=3,
        )
    if role not in wired_roles:
        raise CliRefuse(
            f"planner set {need_label}.needed=true but {role} role is not wired",
            code=5,
        )
    if not LlmAgentAdapter.supports_role(role):
        raise CliRefuse(
            f"{role} declared in --wired-roles but LlmAgentAdapter has no output schema; "
            f"hard dep: adapter role + catalog/profile (#65) / sandbox (#150) — "
            f"do not stub science",
            code=5,
        )
    if role == _ROLE_HYP:
        await _run_wired_hyp_stage(
            rt,
            adapter=adapter,
            ctx=ctx,
            role_budget=role_budget,
            usages=usages,
            task_key=task_key,
            control=control,
        )
        return
    if role == _ROLE_LIT:
        await _run_wired_lit_stage(
            rt,
            adapter=adapter,
            tools=tools,
            role_budget=role_budget,
            usages=usages,
            task_key=task_key,
            study_id=study_id,
            registered_inputs=registered_inputs,
            lit_tool_ids=lit_tool_ids,
            control=control,
        )
        return
    raise CliRefuse(f"role {role!r} has no wired need-stage runner (drift)", code=5)


async def _run_wired_hyp_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    control: StudyControl | None = None,
) -> None:
    """HYP stage via the hypothesizer role: StudyContext in, HypothesisOut journaled.

    HITL v2 §5 gate 2: with a live control the gate (and its one repair round)
    sits between the validated adapter output and the journal record, so the
    accepted output is the one that closes the HYP attempt (same attempt).
    """
    aid = await _require_attempt(rt, task_key)
    while True:
        try:
            hyp_run = await adapter.run(_ROLE_HYP, ctx, [], role_budget)
            break
        except LlmError as exc:
            # ATTEMPT-level failure (see the SPEC lane): retryable attempt
            # close via the core, fresh attempt re-drives the role call;
            # exhaustion → honest typed refuse (rc=4), never a run crash.
            if not await _fail_stage_attempt_retryable(
                rt,
                aid,
                role=_ROLE_HYP,
                task_key=task_key,
                detail=f"llm error: {exc}",
                usages=usages,
            ):
                raise CliRefuse(
                    f"hypothesizer llm error after the {task_key} attempt budget was exhausted: {exc}",
                    code=4,
                ) from exc
            aid = await _require_attempt(rt, task_key)
    usages.append(_usage_row(_ROLE_HYP, hyp_run, task_key=task_key))
    if hyp_run.error is not None:
        raise CliRefuse(f"hypothesizer error: {hyp_run.error}", code=4)
    if not isinstance(hyp_run.output, HypothesisOut):
        raise CliRefuse("hypothesizer must return HypothesisOut", code=4)
    output = hyp_run.output
    if control is not None:
        question = f"Принять гипотезы исследования ({len(output.hypotheses)} шт.)?"
        output = (
            await _stage_gate_review(
                rt, control,
                kind="hypotheses", task_key=task_key, attempt_id=aid,
                question=question,
                output=output,
                snapshot=lambda out: out.model_dump(mode="json"),
                run_repair=_ctx_role_repair(
                    adapter, role=_ROLE_HYP, ctx=ctx, role_budget=role_budget,
                    usages=usages, output_cls=HypothesisOut,
                ),
                usages=usages,
                gate_label="hypotheses gate",
            )
        )[0]
    await record_hypotheses(rt, aid, output)


async def _run_wired_lit_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    tools: CliToolSurface,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    lit_tool_ids: tuple[str, ...],
    control: StudyControl | None = None,
) -> None:
    """LIT stage via the researcher role: planned tool calls → LiteratureOut.

    Tool calls go through the planned surface exactly like executor tasks
    (profile role tools resolved to exact surface ids; call args come from the
    cassette / --tool-call-args). ``record_literature`` refuses papers that do
    not trace to journaled tool results (no invented citations).

    HITL v2 §5 gate 4: with a live control the gate (and its one repair round,
    which re-runs the researcher with the human text and fresh tool receipts)
    sits before the journal record.
    """
    if not lit_tool_ids:
        raise CliRefuse(
            "researcher declared in --wired-roles but the profile researcher role "
            "declares no literature tools (fail-closed: LIT runs through tools, "
            "never model-invented papers)",
            code=5,
        )
    lit_draft = TaskSpecDraft(
        task_key=task_key,
        title=task_key,
        goal="literature search for the study request (wired researcher role)",
        route="literature",
        depends_on=(),
        addresses=(),
        tools_hint=lit_tool_ids,
        expected_outputs=(),
        criteria=(),
    )
    # Resolve planned call args BEFORE start_attempt (no dangling AttemptStarted).
    try:
        planned = tools.planned_calls(lit_tool_ids)
    except KeyError as exc:
        missing = exc.args[0]
        raise CliRefuse(
            f"LIT stage tools_hint missing declared call args for {missing!r} (fail-closed before start_attempt)",
            code=3,
        ) from exc
    aid = await _require_attempt(rt, task_key)
    study_request = ""
    if rt.study is not None:
        study_request = rt.study.request
    while True:
        # Rebuilt per attempt: the route input carries attempt_id, so a
        # retryable re-drive must bind tool receipts to the FRESH attempt
        # (stale ids would orphan the receipts the record path binds).
        lit_input: AutomaticExecutorIn | ExecutorRouteIn
        if isinstance(tools, LiveCliToolSurface) and tools.automatic:
            lit_input = AutomaticExecutorIn(
                mode="automatic",
                request=study_request,
                task=lit_draft,
                study_id=study_id,
                attempt_id=aid,
                inputs=tuple(registered_inputs),
            )
        else:
            lit_input = ExecutorRouteIn(
                task=lit_draft,
                study_id=study_id,
                attempt_id=aid,
                tool_args=planned,
                inputs=tuple(registered_inputs),
            )
        try:
            lit_run = await adapter.run(_ROLE_LIT, lit_input, list(tools.named_tools()), role_budget)
            break
        except LlmError as exc:
            # ATTEMPT-level failure (see the SPEC lane): a big-synthesis
            # researcher call that outlives the transport timeout closes the
            # LIT attempt retryable via the core; the fresh attempt re-runs
            # the planned tool calls + role call. Exhaustion → honest typed
            # refuse (rc=4), never the wave-6 run-crash (st_idem_4d89).
            if not await _fail_stage_attempt_retryable(
                rt,
                aid,
                role=_ROLE_LIT,
                task_key=task_key,
                detail=f"llm error: {exc}",
                usages=usages,
            ):
                raise CliRefuse(
                    f"researcher llm error after the {task_key} attempt budget was exhausted: {exc}",
                    code=4,
                ) from exc
            aid = await _require_attempt(rt, task_key)
    usages.append(_usage_row(_ROLE_LIT, lit_run, task_key=task_key))
    if lit_run.error is not None:
        raise CliRefuse(f"researcher error: {lit_run.error}", code=4)
    if not isinstance(lit_run.output, LiteratureOut):
        raise CliRefuse("researcher must return LiteratureOut", code=4)

    async def repair_lit(feedback: str) -> AgentRun:
        repair_run = await adapter.run(
            _ROLE_LIT, lit_input, list(tools.named_tools()), role_budget, repair_feedback=feedback,
        )
        usages.append(_usage_row(_ROLE_LIT, repair_run, task_key=task_key))
        if repair_run.error is not None:
            raise CliRefuse(f"researcher error: {repair_run.error}", code=4)
        if not isinstance(repair_run.output, LiteratureOut):
            raise CliRefuse("researcher must return LiteratureOut", code=4)
        return repair_run

    accepted_run = lit_run
    if control is not None:
        question = f"Принять найденную литературу ({len(lit_run.output.papers)} стат.)?"

        def lit_snapshot(run: AgentRun) -> dict[str, object]:
            if not isinstance(run.output, LiteratureOut):
                raise CliRefuse("researcher must return LiteratureOut", code=4)
            return run.output.model_dump(mode="json")

        accepted_run = (
            await _stage_gate_review(
                rt, control,
                kind="literature", task_key=task_key, attempt_id=aid,
                question=question,
                output=lit_run,
                snapshot=lit_snapshot,
                run_repair=repair_lit,
                usages=usages,
                gate_label="literature gate",
            )
        )[0]
    await record_literature(rt, aid, accepted_run)


async def _optional_report_stage(
    rt: StudyRuntime,
    *,
    wired_roles: frozenset[str],
    keep_open: bool = False,
) -> None:
    """Close the REPORT DAG placeholder; the wired lane runs post-finalize.

    HITL v2 §5 gate 5: the interactive report gate keeps the placeholder
    pending through the run loop — finalize needs all tasks DONE, so the gate
    sits before StudyFinalized by construction (finalize barrier).
    """
    if keep_open:
        return
    if _ROLE_REPORT not in wired_roles:
        await rt.cancel_task("REPORT", reason="cancelled", detail="report agent not wired")
        return
    # Wired: the placeholder still closes here (finalize needs all tasks DONE;
    # post-finalize the journal is closed by I3). The honest report lane then
    # renders from the completed graph projection into the run's CAS.
    await rt.cancel_task("REPORT", reason="cancelled", detail="report deferred to post-finalize lane")


def _report_gate_due(rt: StudyRuntime) -> bool:
    """True when REPORT is the only non-DONE task — the finalize barrier point."""
    report = _task_by_key(rt, "REPORT")
    if report is None or report.state != TaskState.PENDING:
        return False
    return all(
        task.state == TaskState.DONE
        for task in rt.tasks.values()
        if task.spec.task_key != "REPORT"
    )


async def _interactive_report_gate(
    rt: StudyRuntime,
    *,
    control: StudyControl,
    lane: tuple[LlmClient, str],
    role_budget: Budget,
    wall: Clock,
    usages: list[dict[str, object]],
) -> bool:
    """HITL v2 §5 gate 5: report draft → human gate → close REPORT → finalize.

    The REPORT placeholder is pending, so StudyFinalized cannot commit while
    the human reviews the draft (report must exist at any terminal — the
    closure below restores the legacy finalize path). Revise/reject with
    feedback re-renders the docs ONCE with the human text in the science
    payload; any second return finalizes anyway, with every gate feedback
    folded into the final «Замечания человека» section (journal facts).

    Returns True when the gate flow landed the final docs (the post-finalize
    lane must not re-render); False on lane refusal — the legacy post-finalize
    retry keeps its existing rc=8 semantics.
    """
    lane_client, lane_model = lane
    report = _task_by_key(rt, "REPORT")
    if report is None:
        raise CliRefuse("report gate: REPORT task missing (drift)", code=3)
    try:
        _rendered, digest, prose, markdown = await render_report_docs(
            rt, client=lane_client, model_id=lane_model, budget=role_budget, clock=wall,
            usages=usages, human_feedback=await _journal_feedback_rows(rt),
        )
    except CliRefuse as exc:
        # Pre-finalize lane refusal must not leave the study un-finalizable:
        # close the barrier typed; the legacy post-finalize lane retries after
        # finalize (existing rc=8 run refusal, study stays terminal).
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "note": f"report lane refused pre-finalize; finalize barrier closed typed: {exc.detail}",
        })
        await rt.cancel_task(
            "REPORT", reason="cancelled",
            detail=f"report lane refused pre-finalize: {exc.detail[:200]}",
        )
        return False  # audit-ok[except_silent_return]: typed fallback; barrier closed typed; legacy lane retries rc=8
    except LlmError as exc:
        # Same typed fallback for an LLM transport failure in the report lane
        # (big-synthesis documents legitimately outlive tight timeouts): the
        # finalize barrier closes typed, the legacy post-finalize lane retries
        # — no raw LlmTimeoutError may kill the run (wave-6 st_idem_5400).
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "note": f"report lane llm error pre-finalize; finalize barrier closed typed: llm error: {exc}",
        })
        await rt.cancel_task(
            "REPORT", reason="cancelled",
            detail=f"report lane llm error pre-finalize: {str(exc)[:200]}",
        )
        return False  # audit-ok[except_silent_return]: typed fallback; barrier closed typed; legacy lane retries rc=8
    outcome = await control.review_stage(
        rt, kind="report", task_key="REPORT", attempt_id=report.task_id,
        question="Принять научный отчёт исследования?",
        snapshot={"name": SCIENCE_ARTIFACT_NAME, "content": markdown},
    )
    usages.append({
        "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
        "decision": outcome.decision, "feedback": outcome.feedback,
    })
    repaired = False
    while outcome.decision in ("revise", "reject"):
        feedback = outcome.feedback
        if repaired or feedback is None:
            # Finalize anyway (owner law: report exists at any terminal); the
            # fold below already carries this gate's feedback from the journal.
            break
        repaired = True
        _rendered, digest, prose, markdown = await render_report_docs(
            rt, client=lane_client, model_id=lane_model, budget=role_budget, clock=wall,
            usages=usages, human_feedback=await _journal_feedback_rows(rt),
            science_note=feedback,
        )
        outcome = await control.review_stage(
            rt, kind="report", task_key="REPORT", attempt_id=report.task_id,
            question="Принять научный отчёт исследования?",
            snapshot={"name": SCIENCE_ARTIFACT_NAME, "content": markdown},
        )
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "decision": outcome.decision, "feedback": outcome.feedback,
        })
    # Final science markdown re-assembles WITHOUT a new model call: the last
    # render's prose + the complete journal fold (includes this gate's own
    # accept/revise/reject feedback recorded by the owner before returning).
    final_markdown = build_science_report_markdown(
        digest, prose, human_feedback=await _journal_feedback_rows(rt),
    )
    study = rt.study
    if study is None:
        raise CliRefuse("report gate: runtime has no study", code=3)
    artifact_id = put_science_report(rt, study_id=study.study_id, markdown=final_markdown)
    usages.append({
        "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
        "note": "report gate closed; finalize proceeds",
        "decision": outcome.decision, "feedback": outcome.feedback,
        "science_artifact": artifact_id, "repair_rendered": repaired,
    })
    await rt.cancel_task(
        "REPORT", reason="cancelled",
        detail=f"report gate closed ({outcome.decision}); finalize proceeds",
    )
    return True


def _task_by_key(rt: StudyRuntime, task_key: str) -> Task | None:
    """Resolve a plan task_key to its core ``Task`` row (None on drift)."""
    for task in rt.tasks.values():
        if task.spec.task_key == task_key:
            return task
    return None


async def _fail_stage_attempt_retryable(
    rt: StudyRuntime,
    aid: str,
    *,
    role: str,
    task_key: str,
    detail: str,
    usages: list[dict[str, object]],
) -> bool:
    """Close one stage-lane attempt as a retryable failure; report the re-arm.

    Same core contract as :func:`_fail_attempt_retryable` (AttemptClosed
    failed + retryable via ``rt.fail_attempt``), for the stage lanes the run
    driver itself re-drives in place (SPEC/HYP/LIT): a role-call LLM failure
    (transport timeout, …) is ATTEMPT-level, not run-fatal. The core owns the
    budget: ``charged < max_attempts`` re-arms the task (PENDING + retry_at)
    → returns True and the lane starts the fresh attempt; exhaustion writes
    TaskDone(FAILED) → returns False so the lane surfaces the honest typed
    refuse instead of re-starting a DONE task. The error detail stays visible
    in the run summary (the AttemptClosed payload has no detail field).
    """
    current = rt.attempts.get(aid)
    if isinstance(current, Mapping) and current.get("closed") is True:
        # External cancel already closed the attempt; do not reopen or retry.
        return False
    await rt.fail_attempt(aid, retryable=True, close_reason=CloseReason.FAILED)
    usages.append(
        {
            "role": role,
            "task_key": task_key,
            "attempt_id": aid,
            "retryable_error": detail,
        }
    )
    task = _task_by_key(rt, task_key)
    return task is not None and task.state == TaskState.PENDING


