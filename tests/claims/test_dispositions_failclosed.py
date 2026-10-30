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
from coscientist.core.claims.reasons import Disposition, NotAttemptedReason
from coscientist.core.execution.domain import Task


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
