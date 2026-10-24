"""``coscientist run <task>`` — honest study cycle to close (DESIGN §13 / §12.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO, cast

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    ExecutorRouteIn,
    PlannedToolCall,
    PlanOut,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.checks.catalog import load_catalog
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.journal import record_executor_route, record_research_spec, record_stage_pass
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.clock import SystemClock
from coscientist.core.execution.domain import (
    Budget,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.llm.client import LlmClient
from coscientist.llm.config import LlmConfig
from coscientist.tools.router import ToolRouter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coscientist")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Create a study from <task> and run until closed")
    run_p.add_argument("task", type=Path, help="Path to task text file (request body)")
    run_p.add_argument("--data-root", type=Path, required=True)
    run_p.add_argument("--deadline-s", type=float, required=True)
    run_p.add_argument("--catalog-version", type=int, required=True)
    run_p.add_argument("--catalog", type=Path, required=True, help="Catalog JSON path")
    run_p.add_argument("--profile", type=str, required=True)
    run_p.add_argument("--owner", type=str, required=True)
    run_p.add_argument("--llm-config", type=Path, required=True, help="LlmConfig JSON")
    run_p.add_argument("--planner-model", type=str, required=True)
    run_p.add_argument("--executor-model", type=str, required=True)
    run_p.add_argument("--tz-spec-model", type=str, required=True)
    run_p.add_argument(
        "--tools-json",
        type=Path,
        required=True,
        help="JSON object tool_name → result payload for StaticToolRouter (MCP = G7)",
    )
    run_p.add_argument("--study-id", type=str, required=True)
    run_p.add_argument(
        "--require-hypotheses",
        type=_parse_bool,
        required=True,
        help="Study.require_hypotheses (true|false); true needs hypothesizer (not wired)",
    )
    run_p.add_argument("--finalize-reserve-s", type=int, required=True)
    run_p.add_argument("--lease-ttl-s", type=float, required=True)
    run_p.add_argument("--rng-seed", type=int, required=True)
    run_p.add_argument("--max-llm-calls", type=int, required=True)
    run_p.add_argument("--max-tokens", type=int, required=True)
    run_p.add_argument("--max-tool-calls", type=int, required=True)
    run_p.add_argument("--max-ticks", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error(f"unknown command {args.command!r}")
    catalog = load_catalog(args.catalog, kind_registry=default_execution_registry())
    llm_config = LlmConfig.model_validate_json(args.llm_config.read_text(encoding="utf-8"))
    tools_payload = json.loads(args.tools_json.read_text(encoding="utf-8"))
    if not isinstance(tools_payload, dict):
        raise TypeError("--tools-json must be a JSON object")
    llm_clock = WallLlmClock()
    client = LlmClient(
        llm_config,
        HttpxTransport(),
        clock=llm_clock,
        sleep=wall_sleep,
        wait_for=wall_wait_for,
        rng=random.Random(args.rng_seed),
    )
    router: ToolRouter = StaticToolRouter(tools=tools_payload)
    adapter = LlmAgentAdapter(
        client,
        role_models={
            "planner": args.planner_model,
            "executor-route": args.executor_model,
            "tz_spec": args.tz_spec_model,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
    )
    try:
        return asyncio.run(
            run_study(
                task_path=args.task,
                data_root=args.data_root,
                deadline_s=args.deadline_s,
                catalog_version=args.catalog_version,
                catalog=catalog,
                profile=args.profile,
                owner=args.owner,
                adapter=adapter,
                out=sys.stdout,
                study_id=args.study_id,
                require_hypotheses=args.require_hypotheses,
                finalize_reserve_s=args.finalize_reserve_s,
                lease_ttl_s=args.lease_ttl_s,
                max_llm_calls=args.max_llm_calls,
                max_tokens=args.max_tokens,
                max_tool_calls=args.max_tool_calls,
                max_ticks=args.max_ticks,
            )
        )
    except CliRefuse as exc:
        sys.stderr.write(f"cli refuse: {exc.detail}\n")
        raise SystemExit(exc.code) from None


def _parse_bool(raw: str) -> bool:
    key = raw.strip().lower()
    if key == "true":
        return True
    if key == "false":
        return False
    raise argparse.ArgumentTypeError("expected true|false")


async def run_study(
    *,
    task_path: Path,
    data_root: Path,
    deadline_s: float,
    catalog_version: int,
    catalog: Any,
    profile: str,
    owner: str,
    adapter: LlmAgentAdapter,
    out: TextIO,
    study_id: str,
    require_hypotheses: bool,
    finalize_reserve_s: int,
    lease_ttl_s: float,
    max_llm_calls: int,
    max_tokens: int,
    max_tool_calls: int,
    max_ticks: int,
    study_clock: Any | None = None,
) -> int:
    """Create study, run stages → plan → experiments via ``Executor.record``, tick to close."""
    if require_hypotheses:
        raise CliRefuse(
            "require_hypotheses=true needs hypothesizer role (not wired on #146); refuse",
            code=5,
        )
    request_text = task_path.read_text(encoding="utf-8").strip()
    if not request_text:
        raise CliRefuse("task file must be non-empty", code=2)
    if not (isinstance(deadline_s, (int, float)) and deadline_s > 0):
        raise CliRefuse("deadline_s must be > 0", code=2)
    if type(catalog_version) is not int:
        raise CliRefuse("catalog_version must be int", code=2)
    if type(study_id) is not str or not study_id.strip():
        raise CliRefuse("study_id must be a non-empty str", code=2)
    if type(finalize_reserve_s) is not int or finalize_reserve_s < 0:
        raise CliRefuse("finalize_reserve_s must be int >= 0", code=2)
    if not (isinstance(lease_ttl_s, (int, float)) and lease_ttl_s > 0):
        raise CliRefuse("lease_ttl_s must be > 0", code=2)
    if type(max_ticks) is not int or max_ticks < 1:
        raise CliRefuse("max_ticks must be int >= 1", code=2)

    if study_clock is not None:
        wall = study_clock
    else:
        wall = SystemClock()
    deadline = wall.now() + timedelta(seconds=float(deadline_s))
    data_root.mkdir(parents=True, exist_ok=True)
    rt = cast(
        StudyRuntime,
        await open_runtime(
            data_root,
            study_id,
            catalog=catalog,
            clock=wall,
            owner=owner,
            runtime_cls=StudyRuntime,
        ),
    )
    usages: list[dict[str, Any]] = []
    try:
        await rt.create_study(
            profile=profile,
            request=request_text,
            catalog_version=catalog_version,
            deadline=deadline,
            sid=study_id,
            require_hypotheses=require_hypotheses,
            finalize_reserve_s=finalize_reserve_s,
        )
        await rt.ensure_lease(ttl_s=float(lease_ttl_s))

        role_budget = Budget(
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            max_cost_rub=None,
            max_tool_calls=max_tool_calls,
            deadline=deadline,
        )
        ctx = StudyContext(study_id=study_id, request=request_text, profile=profile)

        # SPEC — tz_spec agent → Executor.record (no cancel).
        spec_aid = await _require_attempt(rt, "SPEC")
        spec_run = await adapter.run("tz_spec", ctx, [], role_budget)
        usages.append(_usage_row("tz_spec", spec_run))
        if spec_run.error is not None:
            raise CliRefuse(f"tz_spec error: {spec_run.error}", code=4)
        if not isinstance(spec_run.output, ResearchSpec):
            raise CliRefuse("tz_spec must return ResearchSpec", code=4)
        await record_research_spec(rt, spec_aid, spec_run.output)

        # HYP/LIT — journal SUCCESS when Need will be off; hypothesizer/researcher absent.
        for stage in ("HYP", "LIT"):
            aid = await _require_attempt(rt, stage)
            await record_stage_pass(rt, aid, stage=stage)

        plan_run = await adapter.run("planner", ctx, [], role_budget)
        usages.append(_usage_row("planner", plan_run))
        if plan_run.error is not None:
            raise CliRefuse(f"planner error: {plan_run.error}", code=4)
        plan_out = plan_run.output
        if not isinstance(plan_out, PlanOut):
            raise CliRefuse("planner must return PlanOut", code=4)
        if plan_out.hypotheses.needed:
            raise CliRefuse(
                "planner set hypotheses.needed=true but hypothesizer role is not wired",
                code=5,
            )
        if plan_out.literature.needed:
            raise CliRefuse(
                "planner set literature.needed=true but researcher role is not wired",
                code=5,
            )

        plan_id = await rt.accept_plan(
            tasks=[_to_task_spec(t) for t in plan_out.tasks],
            questions=[
                QuestionSpec(id=q.id, text=q.text, final_type=q.final_type) for q in plan_out.questions
            ],
            hypotheses=plan_out.hypotheses,
            literature=plan_out.literature,
        )
        if not plan_id:
            detail = await _plan_refused_detail(rt)
            if detail:
                msg = f"accept_plan refused plan: {detail}"
            else:
                msg = "accept_plan refused plan: empty plan_id (no PlanRefused detail)"
            raise CliRefuse(msg, code=3)

        for draft in plan_out.tasks:
            dispatchable = {t.spec.task_key for t in rt.iter_dispatchable_tasks()}
            if draft.task_key not in dispatchable:
                raise CliRefuse(
                    f"task {draft.task_key!r} not dispatchable (deps/state); refuse",
                    code=3,
                )
            aid = await _require_attempt(rt, draft.task_key)
            tools = [SimpleNamespace(spec=SimpleNamespace(name=h)) for h in draft.tools_hint]
            exec_run = await adapter.run(
                "executor-route",
                ExecutorRouteIn(
                    task=draft,
                    study_id=study_id,
                    attempt_id=aid,
                    tool_args=tuple(PlannedToolCall(tool_name=h, args=()) for h in draft.tools_hint),
                ),
                tools,
                role_budget,
            )
            usages.append(_usage_row("executor-route", exec_run, task_key=draft.task_key))
            await record_executor_route(rt, aid, exec_run)

        # REPORT is optional (required=False) but all_done needs every task DONE.
        report_aid = await _require_attempt(rt, "REPORT")
        await record_stage_pass(rt, report_aid, stage="REPORT")

        ticks = 0
        while rt.view().phase != "closed":
            if ticks >= max_ticks:
                raise CliRefuse(
                    f"max_ticks={max_ticks} exhausted before phase=closed "
                    f"(phase={rt.view().phase!r}, verdict={rt.view().verdict!r})",
                    code=6,
                )
            await rt.ensure_lease(ttl_s=float(lease_ttl_s))
            await rt.tick()
            ticks += 1

        view = rt.view()
        out.write(
            json.dumps(
                {
                    "study_id": study_id,
                    "phase": view.phase,
                    "verdict": view.verdict,
                    "usage": usages,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        if view.verdict != "fulfilled":
            raise CliRefuse(
                f"study closed with verdict={view.verdict!r} (want fulfilled)",
                code=7,
            )
        return 0
    except CliRefuse:
        raise
    except AgentError as exc:
        raise CliRefuse(f"agent error: {exc.detail}", code=4) from exc
    finally:
        await close_runtime(rt)


async def _require_attempt(rt: StudyRuntime, task_key: str) -> str:
    aid = await rt.start_attempt(task_key)
    if aid is None:
        raise CliRefuse(f"start_attempt({task_key!r}) returned None (refused)", code=3)
    return aid


async def _plan_refused_detail(rt: StudyRuntime) -> str:
    """Last ``PlanRefused.payload.detail`` from the journal, if any."""
    events = await rt.store.read_all()
    for ev in reversed(events):
        if getattr(ev, "type", None) == "PlanRefused":
            payload = getattr(ev, "payload", None)
            if not isinstance(payload, dict):
                continue
            detail = payload.get("detail")
            if type(detail) is str and detail.strip():
                return detail.strip()
    return ""


def _usage_row(role: str, run: AgentRun, *, task_key: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "role": role,
        "prompt_tokens": run.usage.prompt_tokens,
        "completion_tokens": run.usage.completion_tokens,
        "latency_ms": run.usage.latency_ms,
        "tool_calls": [c.model_dump() for c in run.tool_calls],
    }
    if task_key is not None:
        row["task_key"] = task_key
    return row


def _artifact_exists_rubric() -> Rubric:
    """Minimal frozen rubric so artifact questions can reach disposition=answered."""
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="exists",
                    text="main artifact exists",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                )
            ],
            aggregate="all_required",
        )
    )


def _to_task_spec(draft: TaskSpecDraft) -> TaskSpec:
    # Addressing an artifact/both question requires OutputSpec.rubric (plan_validate).
    if draft.addresses:
        rub: Rubric | None = _artifact_exists_rubric()
    else:
        rub = None
    outputs: list[OutputSpec] = []
    for o in draft.expected_outputs:
        if o.name == "main":
            item_rubric = rub
        else:
            item_rubric = None
        outputs.append(
            OutputSpec(
                name=o.name,
                media_type=o.media_type,
                schema_ref=o.schema_ref,
                required=o.required,
                rubric=item_rubric,
            )
        )
    return TaskSpec(
        task_key=draft.task_key,
        title=draft.title,
        goal=draft.goal,
        route=draft.route,
        depends_on=list(draft.depends_on),
        addresses=list(draft.addresses),
        tools_hint=list(draft.tools_hint),
        expected_outputs=outputs,
        criteria=[RequirementRef(template_id=c) for c in draft.criteria],
    )
