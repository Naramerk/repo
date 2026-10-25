"""Exact quality comparison vs task YAML expected fields (plan item 8).

Compares only the fields declared in task data: ``status``, ``exp_tasks``,
``evidence``. No scores, weights, fractions, or other heuristics.
"""

from __future__ import annotations

from bench.models import BenchTask, QualityComparison, QualityObservation


def compare_quality(task: BenchTask, observed: QualityObservation) -> QualityComparison:
    """Exact equality of observed quality against the task's expected quality.

    ``evidence`` compares as an exact (order-sensitive) tuple: same outcomes,
    same counts, same order. ``record_result`` is observation-only and never
    compared.
    """
    status_equal = task.quality.status == observed.status
    exp_tasks_equal = task.quality.exp_tasks == observed.exp_tasks
    evidence_equal = task.quality.evidence == observed.evidence
    return QualityComparison(
        task_id=task.task_id,
        status_equal=status_equal,
        exp_tasks_equal=exp_tasks_equal,
        evidence_equal=evidence_equal,
        equal=status_equal and exp_tasks_equal and evidence_equal,
    )
