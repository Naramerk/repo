"""Shared test helpers for M0."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.events.digest import sha256_hex
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Budget,
    GateConfig,
    HitlPolicy,
    RequirementRef,
    Study,
    StudyConfig,
    TaskSpec,
    ToolCallRef,
)
from coscientist.core.execution.executor import Executor
from coscientist.core.execution.runtime_factory import close_runtime as close_runtime
from coscientist.core.execution.runtime_factory import open_runtime as _open_runtime_core


def make_tool_ref(
    call_id: str,
    tool: str = "local",
    args: str = "",
    *,
    attempt_id: str | None = None,
) -> ToolCallRef:
    """Test helper: ToolCallRef with required attempt_id (infers from call_id)."""
    if attempt_id is None:
        sep = call_id.find(":")
        if sep <= 0:
            raise TypeError(f"make_tool_ref call_id must be attempt_id:n, got {call_id!r}")
        attempt_id = call_id[:sep]
    return ToolCallRef(
        call_id=call_id,
        tool=tool,
        args_digest=sha256_hex(args),
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        attempt_id=attempt_id,
    )

# Explicit test domain numbers (former Study Field defaults — never silent in core).
_TEST_STUDY_CONFIG_KW: dict[str, Any] = {
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

_STUDY_CONFIG_KEYS = frozenset(_TEST_STUDY_CONFIG_KW)

# Explicit HITL gate timeouts (former _default_gates) — tests only.
_TEST_HITL_GATES: dict[str, GateConfig] = {
    "spec_approval": GateConfig(enabled=True, timeout_s=120, on_timeout="delegate"),
    "plan": GateConfig(enabled=True, timeout_s=180, on_timeout="delegate"),
    "plan_approval": GateConfig(enabled=True, timeout_s=180, on_timeout="delegate"),
    "plan_expansion": GateConfig(enabled=True, timeout_s=180, on_timeout="delegate"),
    "hypotheses": GateConfig(enabled=True, timeout_s=1800, on_timeout="delegate"),
    "costly_or_nonreal": GateConfig(enabled=True, timeout_s=1800, on_timeout="fail"),
    "report": GateConfig(enabled=True, timeout_s=1800, on_timeout="delegate"),
    "result_acceptance": GateConfig(enabled=True, timeout_s=1800, on_timeout="delegate"),
    "skip_task": GateConfig(enabled=True, timeout_s=1800, on_timeout="delegate"),
    "budget": GateConfig(enabled=True, timeout_s=1800, on_timeout="delegate"),
    "side_effect": GateConfig(enabled=True, timeout_s=1800, on_timeout="fail"),
    "proposed_check": GateConfig(enabled=True, timeout_s=1800, on_timeout="fail"),
}


def default_test_study_config(**overrides: Any) -> StudyConfig:
    """Typed StudyConfig with explicit test values (no core Field defaults)."""
    kw = dict(_TEST_STUDY_CONFIG_KW)
    kw.update(overrides)
    return StudyConfig(**kw)


_TEST_HITL_DECISION_DEFAULTS: dict[str, str] = {
    "plan_approval": "human_timeout",
    "result_acceptance": "accept_computed",
    "skip_task": "deny",
    "side_effect": "deny",
    "proposed_check": "reject",
}


def default_test_budget(**overrides: Any) -> Budget:
    """Budget with explicit test caps (former Budget Field defaults)."""
    kw: dict[str, Any] = {
        "max_llm_calls": 100,
        "max_tokens": 1_000_000,
        "max_tool_calls": 500,
    }
    kw.update(overrides)
    return Budget(**kw)


def default_test_hitl_policy(
    *,
    mode: str = "headless",
    gates: dict[str, GateConfig] | None = None,
    **overrides: Any,
) -> HitlPolicy:
    """HitlPolicy with explicit gate timeouts (no ``_default_gates``)."""
    kw: dict[str, Any] = {
        "mode": mode,
        "gates": dict(gates) if gates is not None else dict(_TEST_HITL_GATES),
        "timeout_s": {},
        "on_timeout": None,
        "defaults": dict(_TEST_HITL_DECISION_DEFAULTS),
    }
    kw.update(overrides)
    return HitlPolicy(**kw)  # type: ignore[arg-type]


def make_create_study_body(**kwargs: Any) -> Any:
    """``CreateStudyBody`` with explicit test config / HITL / deadline."""
    from datetime import UTC, datetime, timedelta

    from coscientist.api.studies import CreateStudyBody

    if "config" not in kwargs:
        kwargs["config"] = default_test_study_config()
    if "hitl_policy" not in kwargs:
        kwargs["hitl_policy"] = default_test_hitl_policy()
    if "deadline" not in kwargs:
        kwargs["deadline"] = datetime.now(UTC) + timedelta(hours=2)
    if "request" not in kwargs:
        kwargs["request"] = "test request"
    if "profile" not in kwargs:
        kwargs["profile"] = "research"
    if "catalog_version" not in kwargs:
        kwargs["catalog_version"] = "test"
    if "inputs" not in kwargs:
        kwargs["inputs"] = []
    return CreateStudyBody(**kwargs)


def make_create_study_body_dict(**kwargs: Any) -> dict[str, Any]:
    """JSON-ready dict for ``parse_create_study_body`` / ``post_studies`` raw bodies."""
    return make_create_study_body(**kwargs).model_dump(mode="json")


def make_study_created_payload(**kwargs: Any) -> dict[str, Any]:
    """Full StudyCreated JSON payload with explicit domain numbers (journal seeds)."""
    from datetime import UTC, datetime, timedelta

    now = kwargs.pop("created_at", None) or datetime.now(UTC)
    deadline = kwargs.pop("deadline", None) or (now + timedelta(hours=2))
    study = make_test_study(
        study_id=kwargs.pop("study_id", "st_seed"),
        profile=kwargs.pop("profile", "research"),
        request=kwargs.pop("request", "q"),
        catalog_version=kwargs.pop("catalog_version", "1.0.0"),
        created_at=now,
        deadline=deadline,
        **kwargs,
    )
    return study.model_dump(mode="json")


def make_test_study(**kwargs: Any) -> Study:
    """Build a ``Study`` with explicit test domain numbers + required identity fields."""
    cfg = default_test_study_config()
    fields = cfg.model_dump()
    hitl = kwargs.pop("hitl_policy", None) or default_test_hitl_policy()
    for k in list(kwargs):
        if k in _STUDY_CONFIG_KEYS:
            fields[k] = kwargs.pop(k)
    return Study(hitl_policy=hitl, **fields, **kwargs)


def _merge_study_config(kwargs: dict[str, Any]) -> StudyConfig:
    config = kwargs.pop("config", None)
    flat = {k: kwargs.pop(k) for k in list(kwargs) if k in _STUDY_CONFIG_KEYS}
    if config is None:
        return default_test_study_config(**flat)
    if flat:
        return config.model_copy(update=flat)
    return config


async def create_study(rt: Any, **kwargs: Any) -> str:
    """Test-facing create_study: injects explicit StudyConfig / HitlPolicy.

    Callers may still pass flat domain kwargs (``finalize_reserve_s=…``); those
    override the typed test config. ``deadline`` and ``hitl_policy`` may be
    omitted — helpers supply explicit test values (never core silent defaults).
    """
    clock = getattr(rt, "clock", None)
    now = clock.now() if clock is not None else datetime.now()
    if kwargs.get("deadline") is None:
        kwargs["deadline"] = now + timedelta(hours=2)
    if kwargs.get("hitl_policy") is None:
        kwargs["hitl_policy"] = default_test_hitl_policy()
    if "min_plan_cost" not in kwargs:
        kwargs["min_plan_cost"] = 0
    kwargs["config"] = _merge_study_config(kwargs)
    return await rt.create_study(**kwargs)




def _wrap_expand_plan(rt: Any) -> Any:
    """Inject explicit reason/assign_keys when tests omit them."""
    orig = getattr(rt, "expand_plan", None)
    if not callable(orig):
        return rt

    async def _bound(*args: Any, **kwargs: Any):
        if "reason" not in kwargs:
            kwargs["reason"] = "test_expand"
        if "assign_keys" not in kwargs:
            kwargs["assign_keys"] = True
        return await orig(*args, **kwargs)

    rt.expand_plan = _bound  # type: ignore[method-assign]
    return rt

def _wrap_accept_plan(rt: Any) -> Any:
    """Inject explicit rollup/revision when tests omit them."""
    orig = getattr(rt, "accept_plan", None)
    if not callable(orig):
        return rt

    async def _bound(tasks, **kwargs: Any):
        if "rollup" not in kwargs:
            kwargs["rollup"] = "all"
        if "revision" not in kwargs:
            kwargs["revision"] = 1
        return await orig(tasks, **kwargs)

    rt.accept_plan = _bound  # type: ignore[method-assign]
    return rt


def _wrap_create_study(rt: Any) -> Any:
    """Bind test create_study so ``rt.create_study(**kw)`` stays ergonomic.

    Core ``StudyRuntime`` (no lifecycle mixin) has no ``create_study`` — leave it.
    """
    orig = getattr(rt, "create_study", None)
    if not callable(orig):
        return rt

    async def _bound(**kwargs: Any) -> str:
        clock = getattr(rt, "clock", None)
        now = clock.now() if clock is not None else datetime.now()
        if kwargs.get("deadline") is None:
            kwargs["deadline"] = now + timedelta(hours=2)
        if kwargs.get("hitl_policy") is None:
            kwargs["hitl_policy"] = default_test_hitl_policy()
        if "min_plan_cost" not in kwargs:
            kwargs["min_plan_cost"] = 0
        kwargs["config"] = _merge_study_config(kwargs)
        return await orig(**kwargs)

    rt.create_study = _bound  # type: ignore[method-assign]
    return rt


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
    Wraps ``create_study`` with explicit test StudyConfig / HitlPolicy.
    """
    # ``runtime_cls=None`` must not bypass the API runtime (setdefault skips None).
    if kwargs.get("runtime_cls") is None:
        kwargs["runtime_cls"] = ApiStudyRuntime
    if kwargs.get("clock") is None:
        from coscientist.core.events.clock import SystemClock

        kwargs["clock"] = SystemClock()
    if kwargs.get("owner") is None:
        from coscientist.core.execution.machine import new_owner

        kwargs["owner"] = new_owner()
    if kwargs.get("registry") is None:
        from coscientist.core.claims.graph import default_execution_registry

        kwargs["registry"] = default_execution_registry()
    rt = await _open_runtime_core(
        data_root,
        study_id,
        catalog=catalog if catalog is not None else minimal_test_catalog(),
        **kwargs,
    )
    return _wrap_expand_plan(_wrap_accept_plan(_wrap_create_study(rt)))


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

    if kwargs.get("runtime_cls") is None:
        from coscientist.experimental.runtime import StudyRuntime as ExpRT

        kwargs["runtime_cls"] = ExpRT
    if kwargs.get("clock") is None:
        from coscientist.core.events.clock import SystemClock

        kwargs["clock"] = SystemClock()
    if kwargs.get("owner") is None:
        from coscientist.core.execution.machine import new_owner

        kwargs["owner"] = new_owner()
    if kwargs.get("registry") is None:
        from coscientist.core.claims.graph import default_execution_registry

        kwargs["registry"] = default_execution_registry()
    rt = await _open_exp(
        data_root,
        study_id,
        catalog=catalog if catalog is not None else minimal_test_catalog(),
        **kwargs,
    )
    return _wrap_expand_plan(_wrap_accept_plan(_wrap_create_study(rt)))


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
        d = rt.attempts[aid]
        return type(
            "Att",
            (),
            {
                "attempt_id": d["attempt_id"],
                "closed": d.get("closed", False),
                "provenance": d.get("provenance", "real"),
                "task_id": d["task_id"],
                "study_id": d["study_id"],
                "inputs": list(d.get("inputs") or []),
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

    from coscientist.core.execution.machine import MAX_ATTEMPTS

    ex = Executor(
        rt.writer,
        catalog,
        arts,
        clock=rt.clock,
        get_attempt=get_attempt,
        get_task=lambda tid: rt.tasks[tid],
        on_events=rt._project_local,
        get_proposals=lambda: getattr(rt, "_check_proposals", {}) or {},
        max_attempts=MAX_ATTEMPTS,
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
        content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
    )
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    if not task.spec.criteria:
        new_spec = task.spec.model_copy(
            update={"criteria": [RequirementRef(template_id="value_match", required=True)]}
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
        tolerate_failed_dep=False,
        depends_on=deps or ["PLAN"],
        criteria=[RequirementRef(template_id="value_match", required=True)],
    )
