"""Fail-closed totality of dispositions() on validation-bypassed Task objects.

Task.spec is a required pydantic field and dispositions() reads it directly. An
object that bypassed validation (model_construct) and misses it raises
AttributeError; the public dispositions() wrapper stays total and degrades to
the honest not_attempted fallback for every known question. (On earlier base
revisions the same input raised the same AttributeError one statement later,
in ``_task_by_key`` — the fail-closed outcome is unchanged.)
"""

from __future__ import annotations

from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.experiment_design import (
    ARTIFACT_KIND_EXPERIMENT_DESIGN,
    MISSING_DESIGN_REASON,
)
from coscientist.core.claims.reasons import Disposition, NotAttemptedReason
from coscientist.core.execution.domain import Task


def _artifact_plans() -> dict[str, dict[str, object]]:
    return {
        "p1": {"questions": [{"id": "q1", "text": "Q?", "final_type": "artifact"}]},
    }


def test_task_without_spec_falls_closed() -> None:
    """A Task bypassing validation without spec degrades to the total fallback."""
    t = Task.model_construct(task_id="t1", reason=None)
    plans: dict[str, dict[str, object]] = {
        "p1": {"questions": [{"id": "q1", "text": "Q?"}]},
    }

    result = dispositions(tasks={"t1": t}, plans=plans)

    assert result["q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert result["q1"]["reason"] == NotAttemptedReason.NO_COUNTING_CLAIMS.value
    assert result["q1"]["formulation"] == "Q?"


def test_artifact_disposal_refs_non_list_stays_empty() -> None:
    """Non-list refs no longer explode into characters: reason_detail stays [].

    Previously the artifact path coerced refs via ``list(refs or [])``, so a
    string value was iterated character-by-character into reason_detail; a
    validated QuestionDisposal always carries a list — anything else now fails
    closed to [].
    """
    result = dispositions(
        tasks={},
        plans=_artifact_plans(),
        question_disposals={
            "q1": {
                "disposition": "needs_more_data",
                "refs": "abc",
                "design_artifact_id": "d1",
            }
        },
        journal_kinds={"d1": ARTIFACT_KIND_EXPERIMENT_DESIGN},
    )

    assert result["q1"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert result["q1"]["reason_detail"] == []


def test_artifact_disposal_non_str_design_artifact_id_fails_closed() -> None:
    """A non-str design_artifact_id no longer unlocks needs_more_data.

    Base passed the raw value through to design_artifact_ok, where it was
    str()-coerced — an int id whose string form was journaled in journal_kinds
    unlocked needs_more_data. Now a non-str id fails the design check:
    not_attempted / missing_experiment_design.
    """
    result = dispositions(
        tasks={},
        plans=_artifact_plans(),
        question_disposals={
            "q1": {
                "disposition": "needs_more_data",
                "design_artifact_id": 123,
            }
        },
        journal_kinds={"123": ARTIFACT_KIND_EXPERIMENT_DESIGN},
    )

    assert result["q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert result["q1"]["reason"] == MISSING_DESIGN_REASON


def test_verdict_disposal_non_str_design_artifact_id_fails_closed() -> None:
    """Same non-str gate on the verdict path (default final_type, no artifact).

    The artifact-path test above covers the explicit_art block; the verdict
    path has its own explicit-disposal block with the same isinstance gate.
    Base passed the raw int through to design_artifact_ok on this path too,
    where str() made it match the journaled "123" entry and the needs_more_data
    disposal stuck. Now: not_attempted / missing_experiment_design.
    """
    result = dispositions(
        tasks={},
        plans={"p1": {"questions": [{"id": "q1", "text": "Q?"}]}},
        question_disposals={
            "q1": {
                "disposition": "needs_more_data",
                "design_artifact_id": 123,
            }
        },
        journal_kinds={"123": ARTIFACT_KIND_EXPERIMENT_DESIGN},
    )

    assert result["q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert result["q1"]["reason"] == MISSING_DESIGN_REASON
