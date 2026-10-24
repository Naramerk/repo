"""Shared test helpers for M0."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol
from unittest.mock import patch

from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.core.events.digest import payload_digest as _payload_digest
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import StoredEvent, UnstampedEvent
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
from tests.checks.minimal_test_catalog import minimal_test_catalog


@contextmanager
def permit_legacy_schema_versions() -> Iterator[None]:
    """Test-only: allow append of non-writer schema_version for legacy-upcast seeds."""

    def _stamp(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
        if type(epoch) is not int or isinstance(epoch, bool):
            raise TypeError(f"epoch must be exact int, got {epoch!r}")
        digest = _payload_digest(unstamped.payload)
        if not digest:
            raise JournalFormatError(
                f"{unstamped.type}: empty payload_digest",
                expected_store_format=None,
                found_store_format=None,
            )
        return StoredEvent(
            seq=unstamped.seq,
            study_id=unstamped.study_id,
            type=unstamped.type,
            entity_key=unstamped.entity_key,
            idem_key=unstamped.idem_key,
            payload=unstamped.payload,
            ts=unstamped.ts,
            schema_version=int(unstamped.schema_version),
            epoch=epoch,
            payload_digest=digest,
        )

    with (
        patch("coscientist.core.events.events.stamp_envelope", _stamp),
        patch("coscientist.core.events.store.stamp_envelope", _stamp),
    ):
        yield

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
        content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
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

async def append_legacy(store: Any, event: Any) -> Any:
    """``store.append`` keeping event.schema_version (legacy upcast seeds)."""
    with permit_legacy_schema_versions():
        return await store.append(event)

async def append_many_legacy(store: Any, events: list[Any], **kwargs: Any) -> Any:
    """``store.append_many`` keeping schema_versions (legacy upcast seeds)."""
    with permit_legacy_schema_versions():
        return await store.append_many(events, **kwargs)


async def writer_append_many_legacy(writer: Any, events: list[Any], **kwargs: Any) -> Any:
    with permit_legacy_schema_versions():
        return await writer.append_many(events, **kwargs)

