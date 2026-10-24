"""Shared test helpers for M0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.executor import Executor
from coscientist.core.execution.runtime_factory import close_runtime as close_runtime
from coscientist.core.execution.runtime_factory import open_runtime as _open_runtime_core


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Test wrapper: injects ``minimal_test_catalog`` when catalog omitted.

    Production ``coscientist.core.execution.runtime_factory.open_runtime`` requires
    ``catalog=`` explicitly and never imports this helper. Defaults to the
    API StudyRuntime (lifecycle mixin) so ``create_study`` is available.
    """
    # ``runtime_cls=None`` must not bypass the API runtime (setdefault skips None).
    if kwargs.get("runtime_cls") is None:
        kwargs["runtime_cls"] = ApiStudyRuntime
    return await _open_runtime_core(
        data_root,
        study_id,
        catalog=catalog if catalog is not None else minimal_test_catalog(),
        **kwargs,
    )


async def open_runtime_experimental(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Test wrapper for experimental factory (catalog required in production)."""
    from coscientist.experimental.runtime_factory import (
        open_runtime as _open_exp,
    )

    return await _open_exp(
        data_root,
        study_id,
        catalog=catalog if catalog is not None else minimal_test_catalog(),
        **kwargs,
    )


class RuntimeLike(Protocol):
    """Minimal surface used by attach_executor / record_success."""

    writer: Any
    clock: Any
    attempts: dict[str, dict[str, Any]]
    tasks: dict[str, Any]
    executor: Any

    def _project_local(self, ev: Any) -> None: ...


def attach_executor(rt: RuntimeLike, tmp: Path) -> tuple[Executor, LocalArtifactStore]:
    # Prefer Executor already wired by open_runtime.make_executor (live get_graph).
    existing = getattr(rt, "executor", None)
    existing_arts = getattr(rt, "artifacts", None)
    if existing_arts is None and existing is not None:
        existing_arts = getattr(existing, "artifacts", None)
    if (
        existing is not None
        and getattr(existing, "get_graph", None) is not None
        and existing_arts is not None
    ):
        return existing, existing_arts

    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp / "arts")

    def get_attempt(aid: str):
        from coscientist.core.events.errors import require_journal_field

        d = rt.attempts[aid]
        return type(
            "Att",
            (),
            {
                "attempt_id": d["attempt_id"],
                "closed": require_journal_field(d, "closed", ctx=aid),
                "charged": require_journal_field(d, "charged", ctx=aid),
                "provenance": require_journal_field(d, "provenance", ctx=aid),
                "task_id": d["task_id"],
                "study_id": d["study_id"],
                "inputs": list(d.get("inputs") or []),
                "agent_output_digest": d.get("agent_output_digest"),
                "n": d.get("n"),
                "allocated_budget_rub": d.get("allocated_budget_rub"),
                "budget_limit_rub": d.get("budget_limit_rub"),
            },
        )()

    make = getattr(rt, "make_executor", None)
    if callable(make):
        ex = make(
            catalog,
            arts,
            get_attempt=get_attempt,
            get_task=lambda tid: rt.tasks[tid],
            on_events=rt._project_local,
        )
        return ex, arts

    ex = Executor(
        rt.writer,
        catalog,
        arts,
        clock=rt.clock,
        get_attempt=get_attempt,
        get_task=lambda tid: rt.tasks[tid],
        on_events=rt._project_local,
        get_proposals=lambda: getattr(rt, "_check_proposals", {}) or {},
    )
    ex._runtime_attempts = rt.attempts  # type: ignore[attr-defined]
    bind = getattr(rt, "bind_executor", None)
    if callable(bind):
        bind(ex)
    else:
        ex.get_graph = lambda: rt.graph
        rt.executor = ex
    return ex, arts


async def record_success(
    rt: RuntimeLike, aid: str | None, arts: LocalArtifactStore, *, count: int = 225
) -> None:
    assert rt.executor is not None
    if aid is None:
        raise AssertionError(
            "start_attempt returned None (task refused; no AttemptStarted)"
        )
    content = json.dumps({"count": count}).encode()
    art = arts.put(
        content,
        make_tool_ref(f"{aid}:1", attempt_id=aid),
        name="main",
        media_type="application/json",
        role="data",
    )
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    if not task.spec.criteria:
        new_spec = task.spec.model_copy(
            update={"criteria": [RequirementRef(template_id="value_match")]}
        )
        rt.tasks[task.task_id] = task.model_copy(update={"spec": new_spec})
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": count},
        ),
    )


def exp_spec(key: str, deps: list[str] | None = None) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=key,
        goal="g",
        route="experiment",
        depends_on=deps or ["PLAN"],
        criteria=[RequirementRef(template_id="value_match")],
    )


REQUIRED_STUDY_CAPS: dict[str, Any] = {
    "finalize_reserve_s": 30,
    "confirmations_needed": 1,
    "refutations_needed": 1,
    "refutation_priority": False,
    "supported_needed": 2,
    "aggregate": "count",
    "evalue_alpha": 0.05,
    "max_expansions": 2,
    "max_human_expansions": 10,
    "replan_reserve": 0.1,
    "max_tasks": 64,
    "max_depth": 8,
    "plan_timeout_s": 600,
    "max_foreach_children": 32,
    "max_loop_iter": 8,
    "max_task_budget_share": 1.0,
    "max_proposals": 3,
}


def create_study_body(**kwargs: Any) -> Any:
    """Build ``CreateStudyBody`` with explicit required caps (no wire defaults)."""
    from datetime import UTC, datetime

    from coscientist.api.studies import CreateStudyBody

    base: dict[str, Any] = {
        "profile": "research",
        "request": "q",
        "catalog_version": "test",
        # Fixed deadline so Idempotency-Key digests stay stable across workers.
        "deadline": datetime(2099, 1, 1, tzinfo=UTC),
        **REQUIRED_STUDY_CAPS,
    }
    base.update(kwargs)
    return CreateStudyBody(**base)


async def create_study(rt: Any, **kwargs: Any) -> str:
    """Test helper: merge explicit REQUIRED_STUDY_CAPS (tests must not rely on Study defaults).

    If ``deadline`` is omitted, uses ``rt.clock.now() + 2h`` so call sites stay
    focused on caps — production ``create_study`` still requires deadline.
    """
    from datetime import timedelta

    merged = {**REQUIRED_STUDY_CAPS, **kwargs}
    if merged.get("deadline") is None:
        clock = getattr(rt, "clock", None)
        if clock is None:
            raise ValueError("deadline required for create_study (test helper)")
        merged["deadline"] = clock.now() + timedelta(hours=2)
    return await rt.create_study(**merged)
