"""Build an experimental StudyRuntime (core factory + experimental class)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coscientist.core.claims.graph import KindRegistry
from coscientist.core.events.clock import Clock
from coscientist.core.execution.runtime_factory import close_runtime
from coscientist.core.execution.runtime_factory import open_runtime as open_runtime_core
from coscientist.experimental.runtime import StudyRuntime

__all__ = ["StudyRuntime", "close_runtime", "open_runtime"]


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Any,
    owner: str,
    clock: Clock,
    registry: KindRegistry,
    runtime_cls: type[StudyRuntime] | None = None,
    acquire: bool | None = None,
    artifacts: Any | None = None,
) -> StudyRuntime:
    """Open experimental runtime; clock/owner/registry/catalog required."""
    if runtime_cls is None:
        cls: type[StudyRuntime] = StudyRuntime
    else:
        cls = runtime_cls
    rt = await open_runtime_core(
        data_root,
        study_id,
        catalog=catalog,
        owner=owner,
        clock=clock,
        registry=registry,
        acquire=acquire,
        runtime_cls=cls,
        artifacts=artifacts,
    )
    if not isinstance(rt, StudyRuntime):
        raise TypeError(f"expected experimental StudyRuntime, got {type(rt).__name__}")
    if rt.executor is None:
        raise RuntimeError("executor missing after open_runtime")
    if rt.executor.get_graph is None:
        raise RuntimeError("executor.get_graph missing after open_runtime")
    return rt
