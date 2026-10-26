"""Run-summary line parsing / observed-outcome mapping (shared, single source).

The live CLI lane (``bench.live_runner``) and the driver lanes
(``bench.drivers.web`` / ``bench.drivers.recovery``) read the SAME declared
run-summary output, so the parse and the mapping live here once. Honest
accounting rules (unchanged from plan item 13):

- Usage rows: only rows carrying the LLM token keys (``prompt_tokens`` and
  ``completion_tokens``) are LLM calls (the ``_usage_row`` shape, lane rows
  included). Rows without them (coder job rows, retry notes, critic-issue
  notes) are not LLM calls and are not counted; tokens are never estimated.
- Verdict mapping: the run's verdict (including non-fulfilled ones) is the
  OBSERVED quality status; the exact compare against task data happens in
  ``bench.quality``. A null verdict (study closed without one) raises.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    QualityObservation,
    RunnerSuccess,
    UsageRecorded,
)
from coscientist.llm.types import Usage

# Summary usage rows carrying both token keys are the LLM-call rows (the
# ``_usage_row`` shape; report/review lanes emit the same token keys).
USAGE_TOKEN_KEYS = ("prompt_tokens", "completion_tokens")


def stderr_tail(stderr: str) -> str:
    """Last non-empty stderr line (CLI refusals print one 'cli refuse: ...' line)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "no stderr output"


def parse_summary(stdout: str) -> dict[str, Any]:
    """Final non-empty stdout line must be the run-summary JSON object."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("stdout has no summary line")
    loaded = json.loads(lines[-1])
    if not isinstance(loaded, dict):
        raise ValueError("summary line is not a JSON object")
    return loaded


def usage_rows_to_recorded(summary: Mapping[str, Any], study_id: str) -> tuple[UsageRecorded, ...]:
    """Map LLM token rows of a summary onto ``UsageRecorded`` payloads.

    Rows without both token keys are not LLM calls (coder job rows, retry and
    critic-issue notes) and are skipped — the skip is the accounting rule above,
    never a silent drop of tokens. ``task_id`` is the study-graph task key when
    the row declares one; ``attempt_id`` is not part of the summary rows.
    """
    records: list[UsageRecorded] = []
    for row in summary["usage"]:
        if not all(key in row for key in USAGE_TOKEN_KEYS):
            continue
        records.append(
            UsageRecorded(
                study_id=study_id,
                task_id=row["task_key"] if "task_key" in row else None,
                attempt_id=None,
                role=row["role"],
                usage=Usage(
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    latency_ms=row["latency_ms"] if "latency_ms" in row else 0.0,
                ),
            )
        )
    return tuple(records)


def evidence_rows(summary: Mapping[str, Any]) -> tuple[EvidenceOutcomeCount, ...]:
    return tuple(
        EvidenceOutcomeCount(outcome=row["outcome"], count=row["count"]) for row in summary["quality"]["evidence"]
    )


def success_from_summary(task: BenchTask, summary: Mapping[str, Any]) -> RunnerSuccess:
    """Observed-quality ``RunnerSuccess`` parsed from the run-summary output."""
    study_id = str(summary["study_id"])
    verdict = summary["verdict"]
    if verdict is None:
        raise ValueError("summary verdict is null (study closed without a verdict)")
    return RunnerSuccess(
        kind="success",
        usage_recorded=usage_rows_to_recorded(summary, study_id),
        quality=QualityObservation(
            status=str(verdict),
            exp_tasks=summary["quality"]["exp_tasks"],
            evidence=evidence_rows(summary),
            record_result=summary["quality"]["record_result"],
        ),
    )


__all__ = [
    "USAGE_TOKEN_KEYS",
    "evidence_rows",
    "parse_summary",
    "stderr_tail",
    "success_from_summary",
    "usage_rows_to_recorded",
]
