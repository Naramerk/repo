"""Adapter for the reference (old) CoScientist system.

The upstream reference is ``aimclub/CoScientist_dev`` branch ``dev_1``. That
checkout may be inaccessible from this environment. Point the adapter at a
local clone via ``OldRunnerConfig`` (see ``docs/BENCHMARK.md``).

This module does not guess entrypoints from directory shape: ``entrypoint``
must be an explicit ``module:attr`` that resolves to an async callable
``(task: BenchTask) -> RunnerRawResult``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bench.errors import BenchAdapterError, BenchConfigError
from bench.models import BenchTask, RunnerRawResult, RunnerRefuse, RunnerSuccess

OldTaskCallable = Callable[[BenchTask], Awaitable[RunnerRawResult]]


class OldRunnerConfig(BaseModel):
    """How to locate and invoke the old-system runner.

    All fields are required. There is no default checkout path or entrypoint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_id: str = Field(min_length=1)
    checkout_path: str = Field(min_length=1)
    expected_branch: str = Field(min_length=1)
    verify_branch: bool
    entrypoint: str = Field(
        min_length=1,
        description="Import path module:attr for an async (BenchTask) -> RunnerRawResult",
    )

    @field_validator("system_id", "checkout_path", "expected_branch", "entrypoint")
    @classmethod
    def _no_surrounding_whitespace(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_form(cls, v: str) -> str:
        if v.count(":") != 1:
            raise ValueError("entrypoint must be exactly 'module:attr'")
        module, attr = v.split(":")
        if not module or not attr:
            raise ValueError("entrypoint module and attr must be non-empty")
        if any(ch.isspace() for ch in v):
            raise ValueError("entrypoint must not contain whitespace")
        return v


def _verify_branch(root: Path, expected_branch: str) -> None:
    git_dir = root / ".git"
    if not git_dir.exists():
        raise BenchAdapterError(
            f"verify_branch=true but checkout has no .git: {root}"
        )
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BenchAdapterError(
            f"git rev-parse failed in {root}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    actual = proc.stdout.strip()
    if actual != expected_branch:
        raise BenchAdapterError(
            f"old checkout branch {actual!r} != expected_branch {expected_branch!r}"
        )


def _load_entrypoint(config: OldRunnerConfig) -> OldTaskCallable:
    root = Path(config.checkout_path)
    if not root.is_dir():
        raise BenchAdapterError(
            f"old checkout_path is not a directory: {config.checkout_path!r}"
        )
    if config.verify_branch:
        _verify_branch(root, config.expected_branch)
    root_str = str(root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    module_name, attr_name = config.entrypoint.split(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BenchAdapterError(
            f"failed to import old entrypoint module {module_name!r} from "
            f"{config.checkout_path!r}: {exc}"
        ) from exc
    try:
        target: Any = getattr(module, attr_name)
    except AttributeError as exc:
        raise BenchAdapterError(
            f"old entrypoint attr {attr_name!r} missing on module {module_name!r}"
        ) from exc
    if not callable(target):
        raise BenchAdapterError(
            f"old entrypoint {config.entrypoint!r} is not callable"
        )
    return target  # type: ignore[no-any-return]


class OldCoScientistRunner:
    """``BenchRunner`` that delegates to an explicit old-checkout entrypoint."""

    def __init__(self, config: OldRunnerConfig) -> None:
        self._config = config
        self._call: OldTaskCallable | None = None
        self._init_error: BenchAdapterError | None = None
        try:
            self._call = _load_entrypoint(config)
        except BenchAdapterError as exc:
            self._init_error = exc

    @property
    def system_id(self) -> str:
        return self._config.system_id

    async def run_task(self, task: BenchTask) -> RunnerRawResult:
        if self._init_error is not None:
            return RunnerRefuse(
                kind="refused",
                reason_code="old_adapter_unavailable",
                detail=str(self._init_error),
            )
        if self._call is None:
            raise BenchConfigError("old runner call target missing after init")
        result = await self._call(task)
        if isinstance(result, (RunnerSuccess, RunnerRefuse)):
            return result
        raise BenchAdapterError(
            f"old entrypoint {self._config.entrypoint!r} returned "
            f"{type(result).__name__}, expected RunnerSuccess | RunnerRefuse"
        )
