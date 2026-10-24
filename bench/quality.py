"""Score quality observations against task expectations (exact id/status match)."""

from __future__ import annotations

from bench.models import QualityExpectation, QualityObservation, QualityScore


def score_quality(
    expectation: QualityExpectation,
    observation: QualityObservation,
) -> QualityScore:
    """Compare reported fields and plan criteria to the task config.

    Missing report fields and unmet plan criteria are listed explicitly.
    Dispositions are counted as reported; the harness does not infer them.
    """
    present = set(observation.report_fields_present)
    missing = tuple(
        field for field in expectation.required_report_fields if field not in present
    )
    present_count = len(expectation.required_report_fields) - len(missing)

    by_id = {item.criterion_id: item.status for item in observation.plan_criteria}
    unmet: list[str] = []
    met = 0
    for req in expectation.plan_criteria:
        status = by_id.get(req.criterion_id)
        if status is None:
            unmet.append(req.criterion_id)
            continue
        if status != req.required_status:
            unmet.append(req.criterion_id)
            continue
        met += 1

    return QualityScore(
        report_fields_required=len(expectation.required_report_fields),
        report_fields_present=present_count,
        report_fields_missing=missing,
        plan_criteria_required=len(expectation.plan_criteria),
        plan_criteria_met=met,
        plan_criteria_unmet=tuple(unmet),
        verdict_count=len(observation.verdicts),
    )
