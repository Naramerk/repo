"""Driver lanes for the live bench runner (plan gap 3: web / recovery parity).

``BenchTask.driver`` (web | recovery | None — task data, not config) selects
the lane; ``bench.live_runner.LiveRunner`` branches on it after the shared
preflight (journal-exists guard, task-input resolution). Each driver is a
``Driver``: one task in, one typed result out — the same honest contract as
``BenchRunner`` (observed outcomes as ``RunnerSuccess``, infra/wiring
problems as ``RunnerRefuse``, no verdict invention). Config lives entirely in
``LiveRunnerConfig``'s explicit driver fields (no defaults).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bench.drivers.recovery import RecoveryDriver, build_continue_argv
from bench.drivers.web import WebDriver

if TYPE_CHECKING:
    from pathlib import Path

    from bench.live_runner import LiveRunnerConfig
    from bench.models import BenchTask, RunnerRefuse, RunnerSuccess


@runtime_checkable
class Driver(Protocol):
    """One driver task through one lane (web / recovery), typed result out."""

    def run(
        self,
        task: BenchTask,
        *,
        study_id: str,
        input_paths: tuple[Path, ...],
        config: LiveRunnerConfig,
    ) -> RunnerSuccess | RunnerRefuse: ...


__all__ = [
    "Driver",
    "RecoveryDriver",
    "WebDriver",
    "build_continue_argv",
]
