"""Build an experimental StudyRuntime (core factory + experimental class)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coscientist.core.clock import Clock
from coscientist.core.graph import KindRegistry
from coscientist.core.runtime_factory import close_runtime
from coscientist.core.runtime_factory import open_runtime as open_runtime_core
from coscientist.experimental.runtime import StudyRuntime

__all__ = ["StudyRuntime", "close_runtime", "open_runtime"]


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Any,
    owner: str | None = None,
    clock: Clock | None = None,
    registry: KindRegistry | None = None,
    acquire: bool = True,
    runtime_cls: type[StudyRuntime] | None = None,
    artifacts: Any | None = None,
) -> StudyRuntime:
    """Open experimental runtime; ``catalog`` required (forwarded to core)."""
    cls = runtime_cls or StudyRuntime
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
    assert isinstance(rt, StudyRuntime)
    assert rt.executor is not None
    assert rt.executor.get_graph is not None
    return rt
