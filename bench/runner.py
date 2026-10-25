"""BenchRunner Protocol (plan item 5).

Narrow ``runtime_checkable`` contract: one task in, one typed result out
(``RunnerSuccess | RunnerRefuse``). The new-system entrypoint is the agent-layer
Orchestrator (G1/G1b, #66) — the harness does not invent a second runner API.
Adapters arrive with plan items 12 (old system) and 13 (new system); until then
only fakes are registered.
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
