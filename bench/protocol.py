"""Narrow runner Protocol for old and new CoScientist systems.

The agent/MCP layer for the new system is built in parallel. Adapters and the
real runners implement this Protocol; the harness depends only on it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bench.models import BenchTask, RunnerRawResult


@runtime_checkable
class BenchRunner(Protocol):
    """Run one benchmark task and return provider-backed metrics + quality.

    Implementations must:

    - Supply ``tokens`` from provider usage accounting only (never estimate).
    - If provider usage is unavailable, return ``RunnerRefuse`` with
      ``reason_code="tokens_unavailable"``.
    - Report ``llm_calls`` / ``tool_calls`` from observed instrumentation, not
      from prompt/shape heuristics.
    - Populate ``QualityObservation`` from structured system outputs (report
      fields present, plan criterion statuses, question dispositions) without
      inferring missing values.
    """

    @property
    def system_id(self) -> str:
        """Stable id for this system (e.g. ``old`` or ``new`` from harness config)."""

    async def run_task(self, task: BenchTask) -> RunnerRawResult:
        """Execute ``task`` and return success or a typed refusal."""
