"""Adapter for CoScientist-next (this repo).

The agent layer with MCP is built in parallel. Until a callable is injected or
an explicit ``entrypoint`` is configured, this runner returns a typed refusal
(``new_runner_unwired``) rather than guessing a wiring path.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bench.errors import BenchAdapterError, BenchConfigError
from bench.models import BenchTask, RunnerRawResult, RunnerRefuse, RunnerSuccess

NewTaskCallable = Callable[[BenchTask], Awaitable[RunnerRawResult]]


class NewRunnerConfig(BaseModel):
    """Wire the new-system runner via injection or an explicit entrypoint.

    Exactly one of ``injected_callable`` (runtime only) or ``entrypoint`` must
    be provided when constructing the runner. Config loaded from YAML uses
    ``entrypoint`` only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_id: str = Field(min_length=1)
    entrypoint: str | None

    @field_validator("system_id")
    @classmethod
    def _no_surrounding_whitespace(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_form(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        if v.count(":") != 1:
            raise ValueError("entrypoint must be exactly 'module:attr'")
        module, attr = v.split(":")
        if not module or not attr:
            raise ValueError("entrypoint module and attr must be non-empty")
        if any(ch.isspace() for ch in v):
            raise ValueError("entrypoint must not contain whitespace")
        return v


def _load_entrypoint(entrypoint: str) -> NewTaskCallable:
    module_name, attr_name = entrypoint.split(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BenchAdapterError(
            f"failed to import new entrypoint module {module_name!r}: {exc}"
        ) from exc
    try:
        target: Any = getattr(module, attr_name)
    except AttributeError as exc:
        raise BenchAdapterError(
            f"new entrypoint attr {attr_name!r} missing on module {module_name!r}"
        ) from exc
    if not callable(target):
        raise BenchAdapterError(f"new entrypoint {entrypoint!r} is not callable")
    return target  # type: ignore[no-any-return]


class NewCoScientistRunner:
    """``BenchRunner`` for this repo's agent/MCP stack (narrow Protocol surface)."""

    def __init__(
        self,
        config: NewRunnerConfig,
        *,
        injected: NewTaskCallable | None = None,
    ) -> None:
        if injected is not None and config.entrypoint is not None:
            raise BenchConfigError(
                "NewCoScientistRunner: provide either injected callable or "
                "config.entrypoint, not both"
            )
        self._config = config
        self._call: NewTaskCallable | None = injected
        self._init_error: BenchAdapterError | None = None
        if self._call is None and config.entrypoint is not None:
            try:
                self._call = _load_entrypoint(config.entrypoint)
            except BenchAdapterError as exc:
                self._init_error = exc

    @property
    def system_id(self) -> str:
        return self._config.system_id

    async def run_task(self, task: BenchTask) -> RunnerRawResult:
        if self._init_error is not None:
            return RunnerRefuse(
                kind="refused",
                reason_code="new_adapter_unavailable",
                detail=str(self._init_error),
            )
        if self._call is None:
            return RunnerRefuse(
                kind="refused",
                reason_code="new_runner_unwired",
                detail=(
                    "New CoScientist agent/MCP runner is not wired. Pass an "
                    "injected callable or set NewRunnerConfig.entrypoint to "
                    "module:attr implementing async (BenchTask) -> RunnerRawResult. "
                    "See docs/BENCHMARK.md."
                ),
            )
        result = await self._call(task)
        if isinstance(result, (RunnerSuccess, RunnerRefuse)):
            return result
        raise BenchAdapterError(
            f"new entrypoint returned {type(result).__name__}, "
            "expected RunnerSuccess | RunnerRefuse"
        )
