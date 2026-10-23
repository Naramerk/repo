"""Agent-proposed catalog meta-check (M1 PR20 / DESIGN_M1 §16).

Flow:
1. ``propose_check`` preregisters a ``CheckProposal`` (digest fixed) via ``CheckProposed``.
2. Approval is a HITL checkpoint (``CheckApproved`` / ``CheckRejected``); timeout ≡ reject.
   Delegation (``delegated=true``) routes to a separate reviewer attempt — not the proposer.
3. Core alone decides countability: unapproved → ``unapproved_check``; approved executable
   counts like catalog (severe only when trusted); approved rubric is qualitative never-severe;
   delegated approvals count but do not tip ties against catalog claims.
4. Quota ``Study.max_proposals`` (default 3); excess closes the task with ``proposal_quota``.
"""

from __future__ import annotations

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
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import hitl_request_id, proposal_id
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.rubric import freeze_rubric
from coscientist.core.rubric_types import Rubric

ProposalStatus = Literal["proposed", "approved", "rejected"]

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


def proposal_prereg_body(proposal: CheckProposal | Mapping[str, Any]) -> dict[str, Any]:
    """Canonical body hashed at CheckProposed (excludes approval / runtime ids)."""
    if isinstance(proposal, CheckProposal):
        raw = proposal.model_dump(mode="json")
    else:
        raw = dict(proposal)
    # Whitelist only scientific preregistration fields (ignore status/delegated/…).
    return {k: raw[k] for k in sorted(_PREREG_FIELDS) if k in raw}


def compute_proposal_digest(proposal: CheckProposal | Mapping[str, Any]) -> str:
    """sha256 of JCS(prereg body) — fixed before any check result."""
    return sha256_hex(canonical_json(proposal_prereg_body(proposal)))


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


def _proposals_map(runtime: Any) -> dict[str, dict[str, Any]]:
    m = getattr(runtime, "_check_proposals", None)
    if not isinstance(m, dict):
        runtime._check_proposals = {}
        m = runtime._check_proposals
    return m


def proposal_count(runtime: Any) -> int:
    """Number of CheckProposed events folded on this study (quota counter)."""
    return len(_proposals_map(runtime))


def get_proposal(runtime: Any, proposal_id_s: str) -> dict[str, Any] | None:
    return _proposals_map(runtime).get(str(proposal_id_s))


def proposal_is_approved(runtime: Any, proposal_id_s: str) -> bool:
    row = get_proposal(runtime, proposal_id_s)
    return bool(row and row.get("status") == "approved")


def fold_proposed_check_event(runtime: Any, ev: Any) -> None:
    """Fold CheckProposed / CheckApproved / CheckRejected into runtime state."""
    t = getattr(ev, "type", None)
    p = getattr(ev, "payload", None) or {}
    if not isinstance(p, dict):
        return
    proposals = _proposals_map(runtime)
    if t == "CheckProposed":
        pid = str(p.get("proposal_id") or ev.entity_key)
        proposals[pid] = {
            **p,
            "proposal_id": pid,
            "status": "proposed",
            "delegated": False,
            "approved_by": None,
            "rejected_by": None,
            "reject_reason": None,
        }
    elif t == "CheckApproved":
        pid = str(p.get("proposal_id") or ev.entity_key)
        row = dict(proposals.get(pid) or {"proposal_id": pid})
        row.update(
            {
                "status": "approved",
                "delegated": bool(p.get("delegated")),
                "approved_by": p.get("approved_by") or p.get("by"),
                "reviewer_attempt_id": p.get("reviewer_attempt_id"),
                "hitl_request_id": p.get("hitl_request_id"),
            }
        )
        proposals[pid] = row
    elif t == "CheckRejected":
        pid = str(p.get("proposal_id") or ev.entity_key)
        row = dict(proposals.get(pid) or {"proposal_id": pid})
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


def requirement_ref_for_proposal(proposal_id_s: str, *, required: bool = True) -> RequirementRef:
    """Plan criterion pointing at the catalog meta-template + proposal id."""
    return RequirementRef(
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        params={"proposal_id": str(proposal_id_s)},
        required=required,
    )


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
    Exceeding ``max_proposals`` closes the task with ``TaskReason.PROPOSAL_QUOTA`` and
    returns None — StudyFinalized still runs via the normal machine path.
    """
    study = getattr(runtime, "study", None)
    if study is None:
        raise ValueError("propose_check requires an open study")
    sid = study.study_id
    max_p = int(getattr(study, "max_proposals", 3))
    if proposal_count(runtime) >= max_p:
        await _close_task_proposal_quota(runtime, task_id=task_id, attempt_id=attempt_id)
        return None

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
        target=str(target),
        target_kind=target_kind,
        role=role,
        measures=str(measures),
        check=check_obj,
        rubric=rubric_obj,
        threshold=threshold,
        sesoi=sesoi,
        uses_sesoi=uses_sesoi,
        predicts_null=predicts_null,
        predicted_direction=predicted_direction,  # type: ignore[arg-type]
        justification=str(justification),
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
        events.append(
            EventEnvelope(
                study_id=sid,
                type="HitlOpened",
                entity_key=hitl_rid,
                idem_key=make_idem("HitlOpened", hitl_rid),
                payload={**req.model_dump(mode="json"), "proposal_id": pid},
                ts=now,
                schema_version=1,
            )
        )

    append = getattr(runtime, "_append", None)
    if callable(append):
        await append(events)
    else:
        await runtime.writer.append(events)
        on_events = getattr(runtime, "_project_local", None)
        for ev in events:
            if callable(on_events):
                on_events(ev)
            else:
                fold_proposed_check_event(runtime, ev)
    return pid


async def _close_task_proposal_quota(
    runtime: Any, *, task_id: str, attempt_id: str
) -> None:
    """Close the proposing task with proposal_quota (study still finalizes)."""
    from coscientist.core.domain import TaskState

    study = runtime.study
    assert study is not None
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
    append = getattr(runtime, "_append", None)
    if callable(append):
        await append(events)
    else:
        await runtime.writer.append(events)
        on_events = getattr(runtime, "_project_local", None)
        for ev in events:
            if callable(on_events):
                on_events(ev)


async def approve_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    by: Literal["human", "reviewer"] = "human",
    delegated: bool = False,
    reviewer_attempt_id: str | None = None,
    hitl_request_id_s: str | None = None,
) -> None:
    """Write CheckApproved. Core is the only writer that enables counting."""
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    if row.get("status") == "approved":
        return
    if row.get("status") == "rejected":
        raise ValueError(f"proposal already rejected: {proposal_id_s!r}")
    if delegated:
        proposer = str(row.get("proposer_attempt_id") or row.get("attempt_id") or "")
        reviewer = str(reviewer_attempt_id or "")
        if not reviewer:
            raise ValueError("delegated approval requires reviewer_attempt_id")
        if proposer and reviewer == proposer:
            raise ValueError(
                "delegated reviewer must differ from the proposal author attempt"
            )
        by = "reviewer"
    study = runtime.study
    assert study is not None
    now = runtime.clock.now()
    payload = {
        "proposal_id": str(proposal_id_s),
        "proposal_digest": row.get("proposal_digest"),
        "by": by,
        "approved_by": by,
        "delegated": bool(delegated),
        "reviewer_attempt_id": reviewer_attempt_id,
        "hitl_request_id": hitl_request_id_s,
    }
    ev = EventEnvelope(
        study_id=study.study_id,
        type="CheckApproved",
        entity_key=str(proposal_id_s),
        idem_key=make_idem(
            "CheckApproved",
            str(proposal_id_s),
            sha256_hex(canonical_json(payload)),
        ),
        payload=payload,
        ts=now,
        schema_version=1,
    )
    # Close HITL if still open.
    events = [ev]
    if hitl_request_id_s:
        events.append(
            EventEnvelope(
                study_id=study.study_id,
                type="HitlDecided",
                entity_key=hitl_request_id_s,
                idem_key=make_idem("HitlDecided", hitl_request_id_s),
                payload={
                    "request_id": hitl_request_id_s,
                    "decision": "accept",
                    "by": "human" if by == "human" else "policy",
                    "approved_by": by,
                    "delegated_by": "human" if delegated and by == "reviewer" else None,
                    "reason": "proposed_check_approved",
                    "proposal_id": str(proposal_id_s),
                    "spent_rub": 0.0,
                },
                ts=now,
            )
        )
    append = getattr(runtime, "_append", None)
    if callable(append):
        await append(events)
    else:
        await runtime.writer.append(events)
        on_events = getattr(runtime, "_project_local", None)
        for e in events:
            if callable(on_events):
                on_events(e)
            else:
                fold_proposed_check_event(runtime, e)


async def reject_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    by: Literal["human", "reviewer", "policy"] = "human",
    reason: str = "rejected",
    delegated: bool = False,
    hitl_request_id_s: str | None = None,
) -> None:
    """Write CheckRejected (HITL timeout / human reject / reviewer reject)."""
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    if row.get("status") == "rejected":
        return
    if row.get("status") == "approved":
        raise ValueError(f"proposal already approved: {proposal_id_s!r}")
    study = runtime.study
    assert study is not None
    now = runtime.clock.now()
    payload = {
        "proposal_id": str(proposal_id_s),
        "proposal_digest": row.get("proposal_digest"),
        "by": by,
        "rejected_by": by,
        "reason": str(reason),
        "delegated": bool(delegated),
        "hitl_request_id": hitl_request_id_s,
    }
    events = [
        EventEnvelope(
            study_id=study.study_id,
            type="CheckRejected",
            entity_key=str(proposal_id_s),
            idem_key=make_idem(
                "CheckRejected",
                str(proposal_id_s),
                sha256_hex(canonical_json(payload)),
            ),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    ]
    if hitl_request_id_s:
        # Timeout path may already have HitlTimedOut; still record decision close.
        typ = "HitlTimedOut" if reason == "human_timeout" else "HitlDecided"
        if typ == "HitlTimedOut":
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
        else:
            events.append(
                EventEnvelope(
                    study_id=study.study_id,
                    type="HitlDecided",
                    entity_key=hitl_request_id_s,
                    idem_key=make_idem("HitlDecided", hitl_request_id_s),
                    payload={
                        "request_id": hitl_request_id_s,
                        "decision": "reject",
                        "by": "human" if by == "human" else "policy",
                        "reason": str(reason),
                        "proposal_id": str(proposal_id_s),
                        "spent_rub": 0.0,
                    },
                    ts=now,
                )
            )
    append = getattr(runtime, "_append", None)
    if callable(append):
        await append(events)
    else:
        await runtime.writer.append(events)
        on_events = getattr(runtime, "_project_local", None)
        for e in events:
            if callable(on_events):
                on_events(e)
            else:
                fold_proposed_check_event(runtime, e)


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
                "rejected_by": row.get("rejected_by"),
                "reject_reason": row.get("reject_reason"),
                "justification": row.get("justification"),
                "rejected_templates": row.get("rejected_templates") or [],
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
