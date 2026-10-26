"""BenchRunner Protocol (plan item 5).

Narrow ``runtime_checkable`` contract: one task in, one typed result out
(``RunnerSuccess | RunnerRefuse``). Adapters: ``bench.fake`` (deterministic
replay), ``bench.live_runner`` (plan item 13 — the live CoScientist CLI as a
subprocess; the plan's agent-layer Orchestrator composition does not exist,
and the CLI is the sanctioned single-source live path). The old-system adapter
is plan item 12.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bench.models import BenchTask, RunnerRefuse, RunnerSuccess


@runtime_checkable
class BenchRunner(Protocol):
    def run(self, task: BenchTask) -> RunnerSuccess | RunnerRefuse:
        """Run one task.

        Expected unavailability (unimplemented driver, missing checkout, branch
        mismatch) is a typed ``RunnerRefuse`` — not an exception and not a skip.
        """
        ...
