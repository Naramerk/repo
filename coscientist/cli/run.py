"""``coscientist run <task>`` — create study and tick until closed (DESIGN §13 M1)."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO, cast

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.schemas import (
    AgentRun,
    ExecutorRouteIn,
    PlanOut,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.checks.catalog import load_catalog
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.clock import SystemClock
from coscientist.core.execution.domain import (
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
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error(f"unknown command {args.command!r}")
    catalog = load_catalog(args.catalog, kind_registry=default_execution_registry())
    llm_config = LlmConfig.model_validate_json(args.llm_config.read_text(encoding="utf-8"))
    llm_clock = WallLlmClock()
    client = LlmClient(
        llm_config,
        HttpxTransport(),
        clock=llm_clock,
        sleep=wall_sleep,
        wait_for=wall_wait_for,
        rng=random.Random(0),
    )
    # MCP ToolRouter is G7; CLI ships an explicit empty static map (no silent MCP).
    router: ToolRouter = StaticToolRouter(tools={})
    adapter = LlmAgentAdapter(
        client,
        role_models={
            "planner": args.planner_model,
            "executor-route": args.executor_model,
        },
        tool_router=router,
    )
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
        )
    )


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
    study_clock: Any | None = None,
) -> int:
    """Create study, run planner + executor-route, tick until closed or deadline."""
    request_text = task_path.read_text(encoding="utf-8").strip()
    if not request_text:
        raise ValueError("task file must be non-empty")
    if not (isinstance(deadline_s, (int, float)) and deadline_s > 0):
        raise ValueError("deadline_s must be > 0")
    if type(catalog_version) is not int:
        raise TypeError("catalog_version must be int")

    wall = study_clock if study_clock is not None else SystemClock()
    deadline = wall.now() + timedelta(seconds=float(deadline_s))
    data_root.mkdir(parents=True, exist_ok=True)
    study_id = f"run_{task_path.stem}"
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
            require_hypotheses=False,
            finalize_reserve_s=5 if float(deadline_s) > 5 else 1,
        )
        await rt.ensure_lease(ttl_s=max(30.0, float(deadline_s)))

        plan_run = await adapter.run(
            "planner",
            StudyContext(study_id=study_id, request=request_text, profile=profile),
            [],
            None,
        )
        usages.append(_usage_row("planner", plan_run))
        plan_out = plan_run.output
        if not isinstance(plan_out, PlanOut):
            raise TypeError("planner must return PlanOut")

        await _bootstrap_intake(rt)
        plan_id = await rt.accept_plan(
            tasks=[_to_task_spec(t) for t in plan_out.tasks],
            questions=[QuestionSpec(id=q.id, text=q.text, final_type=q.final_type) for q in plan_out.questions],
            hypotheses=plan_out.hypotheses,
            literature=plan_out.literature,
        )
        if not plan_id:
            raise RuntimeError("accept_plan refused plan (empty plan_id)")

        for draft in plan_out.tasks:
            try:
                aid = await rt.start_attempt(draft.task_key)
            except RuntimeError:
                continue
            if aid is None:
                continue
            exec_run = await adapter.run(
                "executor-route",
                ExecutorRouteIn(
                    task=draft,
                    dependency_context={"study_id": study_id, "attempt_id": aid},
                ),
                [],
                None,
            )
            usages.append(_usage_row("executor-route", exec_run, task_key=draft.task_key))

        # Close by deadline: injectable clocks jump once; wall clocks tick until due.
        setter = getattr(wall, "set", None)
        if callable(setter):
            setter(deadline + timedelta(seconds=1))
        else:
            while rt.view().phase != "closed" and wall.now() < deadline:
                await rt.ensure_lease(ttl_s=30.0)
                await rt.tick()
        if rt.view().phase != "closed":
            await rt.ensure_lease(ttl_s=30.0)
            await rt.tick()

        out.write(
            json.dumps(
                {
                    "study_id": study_id,
                    "phase": rt.view().phase,
                    "verdict": rt.view().verdict,
                    "usage": usages,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0 if rt.view().phase == "closed" else 2
    finally:
        await close_runtime(rt)


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


def _to_task_spec(draft: TaskSpecDraft) -> TaskSpec:
    return TaskSpec(
        task_key=draft.task_key,
        title=draft.title,
        goal=draft.goal,
        route=draft.route,
        depends_on=list(draft.depends_on),
        addresses=list(draft.addresses),
        tools_hint=list(draft.tools_hint),
        expected_outputs=[
            OutputSpec(
                name=o.name,
                media_type=o.media_type,
                schema_ref=o.schema_ref,
                required=o.required,
            )
            for o in draft.expected_outputs
        ],
        criteria=[RequirementRef(template_id=c) for c in draft.criteria],
    )


async def _bootstrap_intake(rt: StudyRuntime) -> None:
    """Cancel SPEC then HYP/LIT so accept_plan can run (deps must be DONE)."""
    for key in ("SPEC", "HYP", "LIT"):
        if key not in rt.tasks:
            continue
        aid = await rt.start_attempt(key)
        if aid is None:
            raise RuntimeError(f"bootstrap: start_attempt({key!r}) returned None")
        await rt.cancel_task(key, reason="cancelled")
