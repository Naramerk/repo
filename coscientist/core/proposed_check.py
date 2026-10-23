"""Agent-proposed catalog meta-check (M1 PR20 / DESIGN_M1 §16).

Flow:
1. ``propose_check`` preregisters a ``CheckProposal`` (digest fixed) via ``CheckProposed``
   **before** the evaluating attempt starts and before any ``ResultRecorded`` for that
   task/target. NFKC is applied in ``compute_proposal_digest``.
2. Approval is a HITL checkpoint (``CheckApproved`` / ``CheckRejected``); timeout ≡ reject.
   Delegation schedules ``ReviewScheduled(kind=proposed_check_review)``; a live reviewer
   attempt (role=reviewer, different task) must call ``approve_check`` / ``reject_check``.
3. Core alone decides countability: unapproved → ``unapproved_check``; approved executable
   counts like catalog (severe only when trusted); approved rubric is qualitative never-severe;
   delegated approvals count but do not tip ties against catalog claims (including 0:0).
4. Quota ``Study.max_proposals`` (default 3); excess closes the task with ``proposal_quota``.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import timedelta
from typing import Any, Literal, Mapping

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    CheckProposal,
    CodeCheck,
    HitlRequest,
    RejectedTemplateNote,
    RequirementRef,
    TaskOutcome,
)
from coscientist.core.errors import AttemptClosedError, StudyClosedError
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import hitl_request_id, proposal_id
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.rubric import freeze_rubric
from coscientist.core.rubric_types import Rubric

ProposalStatus = Literal["proposed", "approved", "rejected"]
ApprovalSource = Literal["hitl", "api", "reviewer"]

# Executable kinds allowed inside a proposed CodeCheck (PR17 + simple predicates).
_ALLOWED_PROPOSED_KINDS = frozenset(
    {
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
    }
)


_PREREG_FIELDS = frozenset(
    {
        "target",
        "target_kind",
        "role",
        "measures",
        "check",
        "rubric",
        "threshold",
        "sesoi",
        "uses_sesoi",
        "predicts_null",
        "predicted_direction",
        "justification",
        "rejected_templates",
    }
)


def _nfkc(value: Any) -> Any:
    """NFKC-normalize strings (and nested string leaves) for stable digests."""
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, list):
        return [_nfkc(v) for v in value]
    if isinstance(value, dict):
        return {str(_nfkc(k)): _nfkc(v) for k, v in value.items()}
    return value


def proposal_prereg_body(proposal: CheckProposal | Mapping[str, Any]) -> dict[str, Any]:
    """Canonical body hashed at CheckProposed (excludes approval / runtime ids)."""
    if isinstance(proposal, CheckProposal):
        raw = proposal.model_dump(mode="json")
    else:
        raw = dict(proposal)
    # Whitelist only scientific preregistration fields (ignore status/delegated/…).
    body = {k: raw[k] for k in sorted(_PREREG_FIELDS) if k in raw}
    return _nfkc(body)  # type: ignore[return-value]


def compute_proposal_digest(proposal: CheckProposal | Mapping[str, Any]) -> str:
    """sha256 of JCS(NFKC(prereg body)) — fixed before any check result."""
    return sha256_hex(canonical_json(proposal_prereg_body(proposal)))


def _reject_non_finite_threshold(proposal: CheckProposal) -> None:
    for name in ("threshold", "sesoi"):
        val = getattr(proposal, name, None)
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")
        if isinstance(val, dict):
            for v in val.values():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")


def validate_proposal_content(proposal: CheckProposal) -> None:
    """Fail-closed validation before journal write."""
    if not proposal.target or not str(proposal.target).strip():
        raise ValueError("CheckProposal.target required")
    if not proposal.measures or not str(proposal.measures).strip():
        raise ValueError("CheckProposal.measures required")
    if not proposal.justification or not str(proposal.justification).strip():
        raise ValueError("CheckProposal.justification required")
    if not proposal.rejected_templates:
        raise ValueError(
            "CheckProposal.rejected_templates required "
            "(justify why nearest catalog templates do not fit)"
        )
    for note in proposal.rejected_templates:
        if not note.template_id or not note.reason:
            raise ValueError("rejected_templates entries need template_id and reason")
        if note.template_id == PROPOSED_CHECK_TEMPLATE_ID:
            raise ValueError("cannot reject the proposed_check meta-template itself")
    has_check = proposal.check is not None
    has_rubric = proposal.rubric is not None
    if has_check == has_rubric:
        raise ValueError("CheckProposal needs exactly one of check= or rubric=")
    if proposal.check is not None:
        kind = proposal.check.kind
        if kind not in _ALLOWED_PROPOSED_KINDS:
            raise ValueError(f"proposed CodeCheck kind not allowed: {kind!r}")
        if kind == "rubric":
            raise ValueError("use rubric= for qualitative proposals, not check.kind=rubric")
    if proposal.rubric is not None:
        freeze_rubric(proposal.rubric)  # validates aggregate / ids
    if proposal.role not in ("prediction", "validity"):
        raise ValueError(f"bad CheckProposal.role: {proposal.role!r}")
    if proposal.uses_sesoi and proposal.sesoi is None and proposal.threshold is None:
        raise ValueError("uses_sesoi requires sesoi or threshold")
    _reject_non_finite_threshold(proposal)


def _proposals_map(runtime: Any) -> dict[str, dict[str, Any]]:
    m = getattr(runtime, "_check_proposals", None)
    if not isinstance(m, dict):
        runtime._check_proposals = {}
        m = runtime._check_proposals
    return m


def _reviews_map(runtime: Any) -> dict[str, dict[str, Any]]:
    m = getattr(runtime, "_proposed_check_reviews", None)
    if not isinstance(m, dict):
        runtime._proposed_check_reviews = {}
        m = runtime._proposed_check_reviews
    return m


def proposal_count(runtime: Any) -> int:
    """Number of CheckProposed events folded on this study (quota counter)."""
    return len(_proposals_map(runtime))


def get_proposal(runtime: Any, proposal_id_s: str) -> dict[str, dict[str, Any]] | dict[str, Any] | None:
    return _proposals_map(runtime).get(str(proposal_id_s))


def proposal_is_approved(runtime: Any, proposal_id_s: str) -> bool:
    row = get_proposal(runtime, proposal_id_s)
    return bool(row and row.get("status") == "approved")


def fold_proposed_check_event(runtime: Any, ev: Any) -> None:
    """Fold CheckProposed / CheckApproved / CheckRejected / ReviewScheduled."""
    t = getattr(ev, "type", None)
    p = getattr(ev, "payload", None) or {}
    if not isinstance(p, dict):
        return
    proposals = _proposals_map(runtime)
    reviews = _reviews_map(runtime)
    seq = getattr(ev, "seq", None)
    if t == "CheckProposed":
        pid = str(p.get("proposal_id") or ev.entity_key)
        digest = str(p.get("proposal_digest") or "")
        proposals[pid] = {
            **p,
            "proposal_id": pid,
            "status": "proposed",
            "delegated": False,
            "approved_by": None,
            "rejected_by": None,
            "reject_reason": None,
            "proposed_seq": seq,
            "bound_task_id": str(p.get("task_id") or ""),
            "bound_target": str(p.get("target") or ""),
            "proposal_digest": digest,
        }
    elif t == "CheckApproved":
        pid = str(p.get("proposal_id") or ev.entity_key)
        row = dict(proposals.get(pid) or {"proposal_id": pid})
        if row.get("status") == "rejected":
            # Rejected → approved is forbidden; ignore adversarial journal order.
            return
        expected = str(row.get("proposal_digest") or "")
        got = str(p.get("proposal_digest") or "")
        if expected and got and expected != got:
            return
        row.update(
            {
                "status": "approved",
                "delegated": bool(p.get("delegated")),
                "approved_by": p.get("approved_by") or p.get("by"),
                "approved_source": p.get("source") or p.get("approved_by") or p.get("by"),
                "reviewer_attempt_id": p.get("reviewer_attempt_id"),
                "hitl_request_id": p.get("hitl_request_id"),
                "bound_task_id": str(
                    p.get("bound_task_id") or row.get("bound_task_id") or row.get("task_id") or ""
                ),
                "bound_target": str(
                    p.get("bound_target") or row.get("bound_target") or row.get("target") or ""
                ),
            }
        )
        proposals[pid] = row
        rev = reviews.get(pid)
        if isinstance(rev, dict):
            rev["status"] = "approved"
    elif t == "CheckRejected":
        pid = str(p.get("proposal_id") or ev.entity_key)
        row = dict(proposals.get(pid) or {"proposal_id": pid})
        if row.get("status") == "approved":
            return
        row.update(
            {
                "status": "rejected",
                "delegated": bool(p.get("delegated")),
                "rejected_by": p.get("rejected_by") or p.get("by"),
                "reject_reason": p.get("reason"),
                "hitl_request_id": p.get("hitl_request_id"),
            }
        )
        proposals[pid] = row
        rev = reviews.get(pid)
        if isinstance(rev, dict):
            rev["status"] = "rejected"
    elif t == "ReviewScheduled" and p.get("kind") == "proposed_check_review":
        pid = str(p.get("proposal_id") or ev.entity_key)
        reviews[pid] = {
            **p,
            "proposal_id": pid,
            "status": str(p.get("status") or "pending"),
            "reviewer_attempt_id": p.get("reviewer_attempt_id"),
            "deadline": p.get("deadline"),
        }


def requirement_ref_for_proposal(proposal_id_s: str, *, required: bool = True) -> RequirementRef:
    """Plan criterion pointing at the catalog meta-template + proposal id."""
    return RequirementRef(
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        params={"proposal_id": str(proposal_id_s)},
        required=required,
    )


def _task_has_result(runtime: Any, task_id: str) -> bool:
    results = getattr(runtime, "results", None) or {}
    if not isinstance(results, dict):
        return False
    for row in results.values():
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id") or "") == str(task_id):
            return True
    return False


def _task_has_started_attempt(runtime: Any, task_id: str) -> bool:
    attempts = getattr(runtime, "attempts", None) or {}
    for att in attempts.values():
        if isinstance(att, dict) and str(att.get("task_id") or "") == str(task_id):
            return True
    return False


async def propose_check(
    runtime: Any,
    *,
    target: str,
    role: Literal["prediction", "validity"],
    measures: str,
    justification: str,
    rejected_templates: list[RejectedTemplateNote | Mapping[str, Any]],
    task_id: str,
    attempt_id: str,
    target_kind: Literal["question", "hypothesis"] = "question",
    check: CodeCheck | Mapping[str, Any] | None = None,
    rubric: Rubric | Mapping[str, Any] | None = None,
    threshold: Any = None,
    sesoi: float | None = None,
    uses_sesoi: bool = False,
    predicts_null: bool = False,
    predicted_direction: str = "two_sided",
    open_hitl: bool = True,
    timeout_s: int | None = None,
) -> str | None:
    """Preregister a proposed check. Returns proposal_id, or None if quota closed the task.

    Writes ``CheckProposed`` (and optionally ``HitlOpened`` kind=proposed_check).
    Must run before the evaluating attempt starts and before any result for task/target.
    Exceeding ``max_proposals`` closes the proposing task with ``PROPOSAL_QUOTA``.
    """
    study = getattr(runtime, "study", None)
    if study is None:
        raise ValueError("propose_check requires an open study")
    if getattr(runtime, "_finalized", False):
        raise StudyClosedError(study.study_id, "CheckProposed")
    sid = study.study_id
    max_p = int(getattr(study, "max_proposals", 3))
    if proposal_count(runtime) >= max_p:
        await _close_task_proposal_quota(runtime, task_id=task_id, attempt_id=attempt_id)
        return None

    # Forbid a new proposal for the same task/target after a result is known.
    if _task_has_result(runtime, task_id):
        raise ValueError(
            f"cannot propose_check for task {task_id!r}: result already recorded"
        )
    target_s = unicodedata.normalize("NFKC", str(target)).strip()
    for row in _proposals_map(runtime).values():
        if (
            str(row.get("bound_task_id") or row.get("task_id") or "") == str(task_id)
            and unicodedata.normalize("NFKC", str(row.get("target") or "")).strip()
            == target_s
            and _task_has_result(runtime, task_id)
        ):
            raise ValueError(
                f"cannot propose_check for task/target after result: {task_id}/{target_s}"
            )

    notes: list[RejectedTemplateNote] = []
    for n in rejected_templates:
        if isinstance(n, RejectedTemplateNote):
            notes.append(n)
        else:
            notes.append(RejectedTemplateNote.model_validate(n))

    check_obj: CodeCheck | None = None
    if check is not None:
        check_obj = check if isinstance(check, CodeCheck) else CodeCheck.model_validate(check)
    rubric_obj: Rubric | None = None
    if rubric is not None:
        rubric_obj = rubric if isinstance(rubric, Rubric) else Rubric.model_validate(rubric)
        rubric_obj = freeze_rubric(rubric_obj)

    pid = proposal_id()
    draft = CheckProposal(
        proposal_id=pid,
        target=target_s,
        target_kind=target_kind,
        role=role,
        measures=unicodedata.normalize("NFKC", str(measures)),
        check=check_obj,
        rubric=rubric_obj,
        threshold=threshold,
        sesoi=sesoi,
        uses_sesoi=uses_sesoi,
        predicts_null=predicts_null,
        predicted_direction=predicted_direction,  # type: ignore[arg-type]
        justification=unicodedata.normalize("NFKC", str(justification)),
        rejected_templates=notes,
        study_id=sid,
        task_id=str(task_id),
        attempt_id=str(attempt_id),
        proposer_attempt_id=str(attempt_id),
    )
    validate_proposal_content(draft)
    digest = compute_proposal_digest(draft)
    proposal = draft.model_copy(update={"proposal_digest": digest})

    now = runtime.clock.now()
    events: list[EventEnvelope] = [
        EventEnvelope(
            study_id=sid,
            type="CheckProposed",
            entity_key=pid,
            idem_key=make_idem("CheckProposed", pid, digest),
            payload=proposal.model_dump(mode="json"),
            ts=now,
            schema_version=1,
        )
    ]

    hitl_rid: str | None = None
    from coscientist.core.hitl_guard import supports_hitl

    # Core runtimes reject open HITL on reopen (HitlUnsupported); only open when supported.
    if open_hitl and supports_hitl(runtime):
        hitl_rid = hitl_request_id()
        task = runtime.tasks.get(task_id)
        gate = study.hitl_policy.gate("proposed_check")
        to = timeout_s if timeout_s is not None else gate.timeout_s
        deadline = min(
            now + timedelta(seconds=to),
            getattr(task, "deadline", study.effective_deadline())
            if task
            else study.effective_deadline(),
            study.effective_deadline(),
        )
        req = HitlRequest(
            request_id=hitl_rid,
            kind="proposed_check",
            payload_ref=pid,
            deadline=deadline,
            study_id=sid,
            task_id=str(task_id),
            open=True,
        )
        # payload_ref is the proposal_id; do not add extra fields (HitlRequest
        # is extra=forbid — experimental runtime validates the open payload).
        events.append(
            EventEnvelope(
                study_id=sid,
                type="HitlOpened",
                entity_key=hitl_rid,
                idem_key=make_idem("HitlOpened", hitl_rid),
                payload=req.model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )

    await _append_events(runtime, events)
    return pid


async def _append_events(runtime: Any, events: list[EventEnvelope]) -> None:
    append = getattr(runtime, "_append", None)
    if callable(append):
        await append(events)
        return
    await runtime.writer.append(events)
    on_events = getattr(runtime, "_project_local", None)
    for ev in events:
        if callable(on_events):
            on_events(ev)
        else:
            fold_proposed_check_event(runtime, ev)


async def _close_task_proposal_quota(
    runtime: Any, *, task_id: str, attempt_id: str
) -> None:
    """Close the proposing task with proposal_quota (study still finalizes).

    Safe against late executor record/close: AttemptClosed / TaskDone use stable
    idem keys; a subsequent ``Executor.record`` on the closed attempt raises
    ``AttemptClosedError`` after ``LateWriteRejected``.
    """
    from coscientist.core.domain import TaskState

    study = runtime.study
    assert study is not None
    if getattr(runtime, "_finalized", False):
        raise StudyClosedError(study.study_id, "TaskDone")
    now = runtime.clock.now()
    task = runtime.tasks.get(task_id)
    events: list[EventEnvelope] = []
    if attempt_id and attempt_id in getattr(runtime, "attempts", {}):
        att = runtime.attempts[attempt_id]
        if not att.get("closed"):
            events.append(
                EventEnvelope(
                    study_id=study.study_id,
                    type="AttemptClosed",
                    entity_key=attempt_id,
                    idem_key=make_idem("AttemptClosed", attempt_id, "proposal_quota"),
                    payload={
                        "attempt_id": attempt_id,
                        "close_reason": CloseReason.FAILED.value,
                    },
                    ts=now,
                    schema_version=2,
                )
            )
    if task is not None and task.state != TaskState.DONE:
        events.append(
            EventEnvelope(
                study_id=study.study_id,
                type="TaskDone",
                entity_key=task_id,
                idem_key=make_idem("TaskDone", task_id, "proposal_quota"),
                payload={
                    "task_id": task_id,
                    "outcome": TaskOutcome.FAILED.value,
                    "reason": TaskReason.PROPOSAL_QUOTA.value,
                },
                ts=now,
                schema_version=2,
            )
        )
    if not events:
        return
    try:
        await _append_events(runtime, events)
    except StudyClosedError:
        raise


def _validate_reviewer_attempt(
    runtime: Any,
    *,
    row: Mapping[str, Any],
    reviewer_attempt_id: str,
) -> dict[str, Any]:
    """Reviewer must exist, be live, role=reviewer, different task/agent than proposer."""
    aid = str(reviewer_attempt_id or "")
    if not aid:
        raise ValueError("delegated approval requires reviewer_attempt_id")
    attempts = getattr(runtime, "attempts", None) or {}
    att = attempts.get(aid)
    if not isinstance(att, dict):
        raise ValueError(f"unknown reviewer_attempt_id: {aid!r}")
    proposer = str(row.get("proposer_attempt_id") or row.get("attempt_id") or "")
    if proposer and aid == proposer:
        raise ValueError("delegated reviewer must differ from the proposal author attempt")
    if att.get("closed"):
        raise AttemptClosedError(aid)
    role = str(att.get("attempt_role") or "agent")
    if role != "reviewer":
        raise ValueError(f"reviewer attempt must have attempt_role=reviewer, got {role!r}")
    prop_task = str(row.get("bound_task_id") or row.get("task_id") or "")
    rev_task = str(att.get("task_id") or "")
    if prop_task and rev_task and prop_task == rev_task:
        raise ValueError("reviewer attempt must belong to a different task than the proposer")
    reviews = _reviews_map(runtime)
    pid = str(row.get("proposal_id") or "")
    rev = reviews.get(pid)
    if not isinstance(rev, dict):
        raise ValueError(f"no ReviewScheduled(proposed_check_review) for {pid!r}")
    return att


async def approve_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    source: ApprovalSource,
    delegated: bool = False,
    reviewer_attempt_id: str | None = None,
    hitl_request_id_s: str | None = None,
) -> None:
    """Write CheckApproved. ``source`` is required — ``human`` only via hitl/api."""
    if source not in ("hitl", "api", "reviewer"):
        raise ValueError(f"invalid approval source: {source!r}")
    if delegated and source != "reviewer":
        raise ValueError("delegated approval requires source='reviewer'")
    if not delegated and source == "reviewer":
        raise ValueError("source='reviewer' requires delegated=True")
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    if row.get("status") == "approved":
        return
    if row.get("status") == "rejected":
        raise ValueError(f"proposal already rejected: {proposal_id_s!r}")

    by: Literal["human", "reviewer"]
    if source in ("hitl", "api"):
        by = "human"
        delegated = False
        reviewer_attempt_id = None
    else:
        by = "reviewer"
        _validate_reviewer_attempt(
            runtime, row=row, reviewer_attempt_id=str(reviewer_attempt_id or "")
        )

    study = runtime.study
    assert study is not None
    now = runtime.clock.now()
    bound_task_id = str(row.get("bound_task_id") or row.get("task_id") or "")
    bound_target = str(row.get("bound_target") or row.get("target") or "")
    payload = {
        "proposal_id": str(proposal_id_s),
        "proposal_digest": row.get("proposal_digest"),
        "by": by,
        "approved_by": by,
        "source": source,
        "delegated": bool(delegated),
        "reviewer_attempt_id": reviewer_attempt_id,
        "hitl_request_id": hitl_request_id_s,
        "bound_task_id": bound_task_id,
        "bound_target": bound_target,
    }
    # Idem key = proposal id only (not payload hash) so retries are stable.
    ev = EventEnvelope(
        study_id=study.study_id,
        type="CheckApproved",
        entity_key=str(proposal_id_s),
        idem_key=make_idem("CheckApproved", str(proposal_id_s)),
        payload=payload,
        ts=now,
        schema_version=1,
    )
    events = [ev]
    if hitl_request_id_s and source == "hitl":
        events.append(
            EventEnvelope(
                study_id=study.study_id,
                type="HitlDecided",
                entity_key=hitl_request_id_s,
                idem_key=make_idem("HitlDecided", hitl_request_id_s),
                payload={
                    "request_id": hitl_request_id_s,
                    "decision": "accept",
                    "by": "human",
                    "approved_by": by,
                    "delegated_by": None,
                    "reason": "proposed_check_approved",
                    "proposal_id": str(proposal_id_s),
                    "spent_rub": 0.0,
                },
                ts=now,
            )
        )
    await _append_events(runtime, events)


async def reject_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    source: ApprovalSource | Literal["timeout"] = "hitl",
    reason: str = "rejected",
    delegated: bool = False,
    hitl_request_id_s: str | None = None,
    reviewer_attempt_id: str | None = None,
) -> None:
    """Write CheckRejected (HITL timeout / human reject / reviewer reject)."""
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    if row.get("status") == "rejected":
        return
    if row.get("status") == "approved":
        raise ValueError(f"proposal already approved: {proposal_id_s!r}")
    if source == "reviewer":
        _validate_reviewer_attempt(
            runtime, row=row, reviewer_attempt_id=str(reviewer_attempt_id or "")
        )
    study = runtime.study
    assert study is not None
    now = runtime.clock.now()
    if source == "timeout":
        by: str = "timeout"
    elif source in ("hitl", "api"):
        by = "human"
    else:
        by = "reviewer"
    payload = {
        "proposal_id": str(proposal_id_s),
        "proposal_digest": row.get("proposal_digest"),
        "by": by,
        "rejected_by": by,
        "source": source,
        "reason": str(reason),
        "delegated": bool(delegated),
        "hitl_request_id": hitl_request_id_s,
        "reviewer_attempt_id": reviewer_attempt_id,
    }
    events = [
        EventEnvelope(
            study_id=study.study_id,
            type="CheckRejected",
            entity_key=str(proposal_id_s),
            idem_key=make_idem("CheckRejected", str(proposal_id_s)),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    ]
    if hitl_request_id_s and reason == "human_timeout":
        events.append(
            EventEnvelope(
                study_id=study.study_id,
                type="HitlTimedOut",
                entity_key=hitl_request_id_s,
                idem_key=make_idem("HitlTimedOut", hitl_request_id_s),
                payload={
                    "request_id": hitl_request_id_s,
                    "reason": "human_timeout",
                    "proposal_id": str(proposal_id_s),
                    "spent_rub": 0.0,
                },
                ts=now,
            )
        )
    elif hitl_request_id_s and source == "hitl":
        events.append(
            EventEnvelope(
                study_id=study.study_id,
                type="HitlDecided",
                entity_key=hitl_request_id_s,
                idem_key=make_idem("HitlDecided", hitl_request_id_s),
                payload={
                    "request_id": hitl_request_id_s,
                    "decision": "reject",
                    "by": "human",
                    "reason": str(reason),
                    "proposal_id": str(proposal_id_s),
                    "spent_rub": 0.0,
                },
                ts=now,
            )
        )
    await _append_events(runtime, events)


async def schedule_proposed_check_review(
    runtime: Any,
    proposal_id_s: str,
    *,
    hitl_request_id_s: str | None = None,
    timeout_s: int | None = None,
) -> None:
    """Journal ReviewScheduled(proposed_check_review) with a hard deadline (timeout≡reject)."""
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    study = runtime.study
    assert study is not None
    now = runtime.clock.now()
    gate = study.hitl_policy.gate("proposed_check")
    to = timeout_s if timeout_s is not None else gate.timeout_s
    task = runtime.tasks.get(str(row.get("task_id") or ""))
    deadline = min(
        now + timedelta(seconds=to),
        getattr(task, "deadline", study.effective_deadline())
        if task
        else study.effective_deadline(),
        study.effective_deadline(),
    )
    await _append_events(
        runtime,
        [
            EventEnvelope(
                study_id=study.study_id,
                type="ReviewScheduled",
                entity_key=str(proposal_id_s),
                idem_key=make_idem("ReviewScheduled", str(proposal_id_s), "proposed_check"),
                payload={
                    "kind": "proposed_check_review",
                    "proposal_id": str(proposal_id_s),
                    "hitl_request_id": hitl_request_id_s,
                    "task_id": row.get("task_id"),
                    "proposer_attempt_id": row.get("proposer_attempt_id")
                    or row.get("attempt_id"),
                    "deadline": deadline.isoformat(),
                    "status": "pending",
                },
                ts=now,
                schema_version=1,
            )
        ],
    )


async def consume_proposed_check_review(
    runtime: Any,
    proposal_id_s: str,
    *,
    decision: Literal["approve", "reject"],
    reviewer_attempt_id: str,
    reason: str = "reviewer_decision",
) -> None:
    """Consumer of ReviewScheduled(kind=proposed_check_review): reviewer decides."""
    if decision == "approve":
        await approve_check(
            runtime,
            proposal_id_s,
            source="reviewer",
            delegated=True,
            reviewer_attempt_id=reviewer_attempt_id,
        )
    else:
        await reject_check(
            runtime,
            proposal_id_s,
            source="reviewer",
            reason=reason,
            delegated=True,
            reviewer_attempt_id=reviewer_attempt_id,
        )


async def reject_stale_proposed_check_reviews(runtime: Any) -> list[str]:
    """Timeout≡reject for delegated reviews past deadline. Returns rejected proposal ids."""
    now = runtime.clock.now()
    rejected: list[str] = []
    for pid, rev in list(_reviews_map(runtime).items()):
        if str(rev.get("status") or "pending") != "pending":
            continue
        row = get_proposal(runtime, pid)
        if row is None or row.get("status") != "proposed":
            continue
        dl_raw = rev.get("deadline")
        if not dl_raw:
            continue
        from datetime import datetime

        if isinstance(dl_raw, datetime):
            dl = dl_raw
        else:
            dl = datetime.fromisoformat(str(dl_raw))
        if now <= dl:
            continue
        await reject_check(
            runtime,
            pid,
            source="timeout",
            reason="human_timeout",
            delegated=True,
            hitl_request_id_s=str(rev.get("hitl_request_id") or "") or None,
        )
        rejected.append(pid)
    return rejected


def assert_proposal_preregistered_for_eval(
    row: Mapping[str, Any],
    *,
    attempt_id: str | None,
    task_id: str | None,
    attempts: Mapping[str, Mapping[str, Any]] | None,
) -> str | None:
    """Return exclusion detail if prereg/bind invariants fail; else None.

    CheckProposed must precede the evaluating AttemptStarted; approval is bound
    to one task_id + target verified here so one approval cannot feed several
    claims toward k_sup.
    """
    bound_task = str(row.get("bound_task_id") or row.get("task_id") or "")
    if task_id and bound_task and str(task_id) != bound_task:
        return f"unapproved_check:task_mismatch:{bound_task}!={task_id}"
    proposed_seq = row.get("proposed_seq")
    if attempt_id and attempts and isinstance(proposed_seq, int):
        att = attempts.get(str(attempt_id))
        if isinstance(att, dict):
            start_seq = att.get("_seq")
            if isinstance(start_seq, int) and proposed_seq >= start_seq:
                return (
                    "unapproved_check:prereg_after_attempt:"
                    f"proposed_seq={proposed_seq}>=attempt_seq={start_seq}"
                )
    return None


def code_check_from_proposal(row: Mapping[str, Any]) -> CodeCheck | None:
    """Build the frozen CodeCheck from a folded proposal row (None if rubric)."""
    raw = row.get("check")
    if raw is None:
        return None
    return raw if isinstance(raw, CodeCheck) else CodeCheck.model_validate(raw)


def rubric_from_proposal(row: Mapping[str, Any]) -> Rubric | None:
    raw = row.get("rubric")
    if raw is None:
        return None
    r = raw if isinstance(raw, Rubric) else Rubric.model_validate(raw)
    return freeze_rubric(r)


def custom_checks_report(runtime: Any) -> list[dict[str, Any]]:
    """Report section ``custom_checks[]`` (non-catalog; DESIGN_M1 §16)."""
    out: list[dict[str, Any]] = []
    for pid, row in sorted(_proposals_map(runtime).items()):
        out.append(
            {
                "proposal_id": pid,
                "proposal_digest": row.get("proposal_digest"),
                "target": row.get("target"),
                "target_kind": row.get("target_kind"),
                "role": row.get("role"),
                "measures": row.get("measures"),
                "status": row.get("status"),
                "delegated": bool(row.get("delegated")),
                "approved_by": row.get("approved_by"),
                "approved_source": row.get("approved_source"),
                "rejected_by": row.get("rejected_by"),
                "reject_reason": row.get("reject_reason"),
                "justification": row.get("justification"),
                "rejected_templates": row.get("rejected_templates") or [],
                "bound_task_id": row.get("bound_task_id") or row.get("task_id"),
                "bound_target": row.get("bound_target") or row.get("target"),
                "catalog": False,
                "note": "не каталожная",
            }
        )
    return out


def is_delegated_proposed_claim(claim: Any) -> bool:
    """True when claim evidence comes from an approved delegated proposed_check."""
    effect = getattr(claim, "effect", None)
    if not isinstance(effect, dict):
        return False
    return bool(effect.get("proposed_check") and effect.get("delegated"))


def is_proposed_claim(claim: Any) -> bool:
    effect = getattr(claim, "effect", None)
    if not isinstance(effect, dict):
        return False
    return bool(effect.get("proposed_check"))
