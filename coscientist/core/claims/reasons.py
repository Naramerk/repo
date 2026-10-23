"""Closed reason vocabularies for tasks, attempts, and study finalization (M1)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class TaskReason(StrEnum):
    """Why a task is DONE (FAILED/SKIPPED) or PENDING with a scheduled retry."""

    FAILED = "failed"
    DEADLINE = "deadline"
    STUDY_DEADLINE = "study_deadline"
    CRASHED = "crashed"
    CANCELLED = "cancelled"
    BUDGET = "budget"
    INPUT_UNAVAILABLE = "input_unavailable"
    HUMAN = "human"
    HUMAN_REJECTED = "human_rejected"
    CRITIC_REJECTED = "critic_rejected"
    HUMAN_TIMEOUT = "human_timeout"
    CATALOG_ERROR = "catalog_error"
    SANDBOX_INFRA = "sandbox_infra"
    SANDBOX_TIMEOUT = "sandbox_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    SAME_DIGEST_RETRY = "same_digest_retry"
    CORE_BUG = "core_bug"
    SCHEMA_INVALID = "schema_invalid"
    INVALID_STATUS = "invalid_status"
    RETRY_SCHEDULED = "retry_scheduled"
    # PR 9/11 — declare only in PR 1
    SUPERSEDED = "superseded"
    EXPANSION_LIMIT = "expansion_limit"
    ITERATION_CAP = "iteration_cap"
    REFUSED = "refused"
    # M1 PR20: agent proposed_check quota exhausted (task still closes).
    PROPOSAL_QUOTA = "proposal_quota"


class CloseReason(StrEnum):
    """Attempt.close_reason values."""

    RESULT = "result"
    FAILED = "failed"
    CRASHED = "crashed"
    DEADLINE = "deadline"
    STUDY_DEADLINE = "study_deadline"
    CANCELLED = "cancelled"
    INPUT_UNAVAILABLE = "input_unavailable"
    HUMAN_TIMEOUT = "human_timeout"
    BUDGET = "budget"


class TerminalReason(StrEnum):
    """Study-level terminal reason (payload of StudyFinalized; logic in PR 2)."""

    REFUSED = "refused"
    HUMAN_TIMEOUT = "human_timeout"
    BUDGET = "budget"
    DEADLINE = "deadline"
    EXPANSION_LIMIT = "expansion_limit"
    INFEASIBLE = "infeasible"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    INTERNAL_ERROR = "internal_error"


class Disposition(StrEnum):
    """Per-question disposition (computed in PR 2 / PR 6/8)."""

    ANSWERED = "answered"
    REFUTED = "refuted"
    SURVIVED = "survived"
    INCONCLUSIVE = "inconclusive"
    NEEDS_MORE_DATA = "needs_more_data"
    INFEASIBLE = "infeasible"
    ILL_POSED = "ill_posed"
    NOT_ATTEMPTED = "not_attempted"
    # PR13: covering task ran but rubric rejected (not a true not-attempted).
    RUBRIC_NOT_MET = "rubric_not_met"


class NotAttemptedReason(StrEnum):
    """Why a question is not_attempted (used by dispositions() in PR 2)."""

    NO_TASK = "no_task"
    NO_COUNTING_CLAIMS = "no_counting_claims"
    TASKS_FAILED = "tasks_failed"
    TASKS_SKIPPED = "tasks_skipped"


class RefusalKind(StrEnum):
    """Why intake refused a study (see ``coscientist.core.plan.intake``)."""

    SAFETY = "safety"
    OUT_OF_SCOPE = "out_of_scope"
    EMPTY_REQUEST = "empty_request"
    UNSUPPORTED_OUTPUT = "unsupported_output"
    BUDGET_ZERO = "budget_zero"
    CONTRADICTORY = "contradictory"
    EGRESS_POLICY = "egress_policy"
    INPUT_TOO_LARGE = "input_too_large"
    INPUT_INVALID = "input_invalid"
    # DESIGN §0.5 / PR4: duration ≤ finalize_reserve_s (not in rev5 enum list)
    DURATION_TOO_SHORT = "duration_too_short"


async def intake(
    request: str,
    inputs: list[Any],
    profile: str,
    budget: Any,
    **kwargs: Any,
) -> Any:
    """Delegate to ``coscientist.core.plan.intake.intake`` (PR 4)."""
    from coscientist.core.plan.intake import intake as _intake

    return await _intake(request, inputs, profile, budget, **kwargs)


def coerce_task_reason(value: str | TaskReason | None) -> TaskReason | None:
    """Parse a TaskDone.reason; unknown strings raise ValueError."""
    if value is None:
        return None
    if isinstance(value, TaskReason):
        return value
    return TaskReason(value)


def coerce_close_reason(value: str | CloseReason | None) -> CloseReason | None:
    """Parse an AttemptClosed.close_reason; unknown strings raise ValueError."""
    if value is None:
        return None
    if isinstance(value, CloseReason):
        return value
    return CloseReason(value)
