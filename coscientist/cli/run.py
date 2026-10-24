"""``coscientist run <task>`` — honest study cycle to close (DESIGN §13 / §12.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

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
    RubricDraft,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.checks.catalog import load_catalog
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.journal import record_executor_route, record_research_spec
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import (
    Budget,
    Catalog,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.llm.client import LlmClient
from coscientist.llm.config import LlmConfig
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec


@dataclass(frozen=True)
class _AllowTool:
    """Allow-list entry exposing ``spec.name`` for ``LlmAgentAdapter``."""

    spec: ToolSpec

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        del args, ctx
        raise AssertionError("AllowTool.call must not be invoked; use ToolRouter")


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
    if not isinstance(tools_payload, Mapping):
        raise CliRefuse("--tools-json must be a JSON object", code=2)
    for name, payload in tools_payload.items():
        if type(name) is not str or not name.strip():
            raise CliRefuse("--tools-json keys must be non-empty str", code=2)
        if not isinstance(payload, Mapping):
            raise CliRefuse(f"--tools-json[{name!r}] must be a JSON object", code=2)
    llm_clock = WallLlmClock()
    client = LlmClient(
        llm_config,
        HttpxTransport(),
        clock=llm_clock,
        sleep=wall_sleep,
        wait_for=wall_wait_for,
        rng=random.Random(args.rng_seed),
    )
    router = StaticToolRouter(tools=tools_payload)
    study_clock = SystemClock()
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
        clock=study_clock,
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
                allowed_tool_names=router.tool_names,
                study_clock=study_clock,
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
    catalog: Catalog,
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
    allowed_tool_names: frozenset[str],
    study_clock: Clock | None = None,
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
    if type(max_llm_calls) is not int or max_llm_calls < 1:
        raise CliRefuse("max_llm_calls must be int >= 1", code=2)
    if type(max_tokens) is not int or max_tokens < 1:
        raise CliRefuse("max_tokens must be int >= 1", code=2)
    if type(max_tool_calls) is not int or max_tool_calls < 0:
        raise CliRefuse("max_tool_calls must be int >= 0", code=2)
    if not isinstance(allowed_tool_names, frozenset):
        raise CliRefuse("allowed_tool_names must be frozenset[str]", code=2)
    for name in allowed_tool_names:
        if type(name) is not str or not name.strip():
            raise CliRefuse("allowed_tool_names entries must be non-empty str", code=2)

    if study_clock is not None:
        wall = study_clock
    else:
        wall = SystemClock()
    deadline = wall.now() + timedelta(seconds=float(deadline_s))
    data_root.mkdir(parents=True, exist_ok=True)
    opened = await open_runtime(
        data_root,
        study_id,
        catalog=catalog,
        clock=wall,
        owner=owner,
        runtime_cls=StudyRuntime,
    )
    if not isinstance(opened, StudyRuntime):
        raise CliRefuse(
            f"open_runtime returned {type(opened).__name__}, expected StudyRuntime",
            code=4,
        )
    rt = opened
    usages: list[dict[str, Any]] = []
    allow_tools = [_AllowTool(ToolSpec(name=n)) for n in sorted(allowed_tool_names)]
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

        # SPEC — tz_spec agent → Executor.record.
        spec_aid = await _require_attempt(rt, "SPEC")
        spec_run = await adapter.run("tz_spec", ctx, [], role_budget)
        usages.append(_usage_row("tz_spec", spec_run))
        if spec_run.error is not None:
            raise CliRefuse(f"tz_spec error: {spec_run.error}", code=4)
        if not isinstance(spec_run.output, ResearchSpec):
            raise CliRefuse("tz_spec must return ResearchSpec", code=4)
        await record_research_spec(rt, spec_aid, spec_run.output)

        # Planner before HYP/LIT close so Need is known (no fabricated SUCCESS).
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

        # Need off → official SKIPPED via cancel_task (not fabricated SUCCESS).
        await rt.cancel_task("HYP", reason="cancelled", detail="need_off; hypothesizer not wired")
        await rt.cancel_task("LIT", reason="cancelled", detail="need_off; researcher not wired")

        plan_id = await rt.accept_plan(
            tasks=[_to_task_spec(t) for t in plan_out.tasks],
            questions=[QuestionSpec(id=q.id, text=q.text, final_type=q.final_type) for q in plan_out.questions],
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
            unknown = [h for h in draft.tools_hint if h not in allowed_tool_names]
            if unknown:
                raise CliRefuse(
                    f"task {draft.task_key!r} tools_hint {unknown!r} not in router allow-list",
                    code=3,
                )
            aid = await _require_attempt(rt, draft.task_key)
            # StaticToolRouter refuses non-empty args; empty PlannedToolCall.args is the contract.
            exec_run = await adapter.run(
                "executor-route",
                ExecutorRouteIn(
                    task=draft,
                    study_id=study_id,
                    attempt_id=aid,
                    tool_args=tuple(PlannedToolCall(tool_name=h, args=()) for h in draft.tools_hint),
                ),
                list(allow_tools),
                role_budget,
            )
            usages.append(_usage_row("executor-route", exec_run, task_key=draft.task_key))
            await record_executor_route(rt, aid, exec_run)

        # REPORT is required=False; cancel (SKIPPED) — no fabricated SUCCESS.
        await rt.cancel_task("REPORT", reason="cancelled", detail="report agent not wired")

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
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "PlanRefused":
            continue
        payload = ev.payload
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


def _rubric_from_draft(draft: RubricDraft) -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=it.id,
                    text=it.text,
                    required=it.required,
                    check=it.check,
                    code_kind=it.code_kind,
                )
                for it in draft.items
            ],
            aggregate=draft.aggregate,
            k=draft.k,
        )
    )


def _to_task_spec(draft: TaskSpecDraft) -> TaskSpec:
    """Map planner draft → core ``TaskSpec``. Rubrics come only from the draft."""
    outputs: list[OutputSpec] = []
    for o in draft.expected_outputs:
        if o.rubric is None:
            item_rubric = None
        else:
            item_rubric = _rubric_from_draft(o.rubric)
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


__all__ = ["main", "run_study"]
