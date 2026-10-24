"""Quality scoring is exact id/status match against task config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bench.models import (
    PlanCriterionExpectation,
    PlanCriterionObservation,
    ProviderTokenUsage,
    QualityExpectation,
    QualityObservation,
    VerdictObservation,
)
from bench.quality import score_quality


def test_score_all_met() -> None:
    exp = QualityExpectation(
        required_report_fields=("summary", "verdict"),
        plan_criteria=(
            PlanCriterionExpectation(criterion_id="c1", required_status="MET"),
        ),
    )
    obs = QualityObservation(
        verdicts=(VerdictObservation(question_id="q1", disposition="answered"),),
        plan_criteria=(
            PlanCriterionObservation(criterion_id="c1", status="MET"),
        ),
        report_fields_present=("summary", "verdict", "extra"),
    )
    score = score_quality(exp, obs)
    assert score.report_fields_missing == ()
    assert score.report_fields_present == 2
    assert score.plan_criteria_met == 1
    assert score.plan_criteria_unmet == ()
    assert score.verdict_count == 1


def test_score_missing_field_and_unmet_criterion() -> None:
    exp = QualityExpectation(
        required_report_fields=("summary", "verdict", "sections"),
        plan_criteria=(
            PlanCriterionExpectation(criterion_id="c1", required_status="MET"),
            PlanCriterionExpectation(criterion_id="c2", required_status="MET"),
        ),
    )
    obs = QualityObservation(
        verdicts=(),
        plan_criteria=(
            PlanCriterionObservation(criterion_id="c1", status="UNMET"),
        ),
        report_fields_present=("summary",),
    )
    score = score_quality(exp, obs)
    assert score.report_fields_missing == ("verdict", "sections")
    assert score.report_fields_present == 1
    assert score.plan_criteria_met == 0
    assert score.plan_criteria_unmet == ("c1", "c2")
    assert score.verdict_count == 0


def test_provider_token_total_must_match_parts() -> None:
    with pytest.raises(ValidationError):
        ProviderTokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=99)
