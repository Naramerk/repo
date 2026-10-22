"""Build an experimental StudyRuntime (core factory + experimental class)."""

from __future__ import annotations

from pathlib import Path

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
    owner: str | None = None,
    clock: Clock | None = None,
    registry: KindRegistry | None = None,
    acquire: bool = True,
    runtime_cls: type[StudyRuntime] | None = None,
) -> StudyRuntime:
    cls = runtime_cls or StudyRuntime
    rt = await open_runtime_core(
        data_root,
        study_id,
        owner=owner,
        clock=clock,
        registry=registry,
        acquire=acquire,
        runtime_cls=cls,
    )
    assert isinstance(rt, StudyRuntime)
    return rt
