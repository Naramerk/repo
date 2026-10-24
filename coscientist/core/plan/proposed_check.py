"""Agent-proposed catalog meta-check (M1 PR20 / DESIGN_M1 §16).

Flow:
1. ``propose_check`` preregisters a ``CheckProposal`` (digest fixed) via ``CheckProposed``
   **before** the evaluating attempt starts and before any ``ResultRecorded`` for that
   task/target. NFKC is applied in ``compute_proposal_digest``.
2. Approval is a HITL checkpoint (``CheckApproved`` / ``CheckRejected``); timeout ≡ reject.
   Delegation schedules ``ReviewScheduled(kind=proposed_check_review)`` with a designated
   ``reviewer_task_id``; only a live ``attempt_role=reviewer`` attempt on that task may
   ``bind_proposed_check_reviewer`` then ``approve_check`` / ``reject_check``.
3. Core alone decides countability: unapproved → ``unapproved_check``; approved executable
   counts like catalog (severe only when trusted); approved rubric is qualitative never-severe;
   delegated approvals count but do not tip ties against catalog claims (including 0:0).
4. Quota ``Study.max_proposals`` (default 3) caps **new** proposal ids before eval starts;
   excess closes the proposing task with ``proposal_quota``. Live-attempt / post-result
   gates are separate temporal prereg windows, not a second quota.

Unicode / digests:
   Digests and target binds use NFKC only. Compatibility forms (e.g. fullwidth Latin)
   collapse; **confusables do not** (Cyrillic ``а`` ≠ Latin ``a``). Homoglyph attacks
   that rely on distinct scripts remain distinct proposal digests / target mismatches.
"""

from __future__ import annotations

import asyncio
import math
import unicodedata
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Literal, Mapping

from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import (
    AttemptClosedError,
    InvalidStatus,
    JournalFormatError,
    StudyClosedError,
)
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import hitl_request_id, proposal_id
from coscientist.core.events.mapping import optional_item
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    CHECK_APPROVED_SCHEMA_VERSION,
    CHECK_PROPOSED_SCHEMA_VERSION,
    CHECK_REJECTED_SCHEMA_VERSION,
    HITL_OPENED_SCHEMA_VERSION,
    REVIEW_SCHEDULED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    CheckProposal,
    CodeCheck,
    HitlRequest,
    RejectedTemplateNote,
    RequirementRef,
    TaskOutcome,
)


def _or_empty_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value

def _task_or_study_deadline(task, study):
    if task is not None:
        return task.deadline
    return study.effective_deadline()


def _proposal_row(proposals, pid):
    row = optional_item(proposals, pid)
    if row is not None:
        return row
    return {"proposal_id": pid}



def _or_zero_int(value: object) -> int:
    if value is None:
        return 0
    if any((type(value) is bool, not isinstance(value, int))):
        raise TypeError(f"expected int|None, got {type(value).__name__}")
    return value  # type: ignore[return-value]


_MISSING_BOUND = "bound_*"


def _or_empty_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"expected list|tuple|None, got {type(value).__name__}")


def _or_empty_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected dict|None, got {type(value).__name__}")


def _payload_str(d: Mapping[str, Any], key: str) -> str:
    if key not in d:
        raise KeyError(key)
    return _or_empty_str(d[key])


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
    """NFKC-normalize strings (and nested string leaves) for stable digests.

    Limitation: NFKC does not collapse cross-script confusables (Cyrillic/Latin
    lookalikes, etc.); only Unicode compatibility equivalents (e.g. fullwidth).
    """
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
        val = getattr(proposal, name)
        if isinstance(val, float) and any((math.isnan(val), math.isinf(val))):
            raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")
        if isinstance(val, dict):
            for v in val.values():
                if isinstance(v, float) and any((math.isnan(v), math.isinf(v))):
                    raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")

def validate_proposal_content(proposal: CheckProposal) -> None:
    """Fail-closed validation before journal write."""
    if any((not proposal.target, not proposal.target.strip())):
        raise ValueError("CheckProposal.target required")
    if any((not proposal.measures, not proposal.measures.strip())):
        raise ValueError("CheckProposal.measures required")
    if any((not proposal.justification, not proposal.justification.strip())):
        raise ValueError("CheckProposal.justification required")
    if not proposal.rejected_templates:
        raise ValueError(
            "CheckProposal.rejected_templates required "
            "(justify why nearest catalog templates do not fit)"
        )
    for note in proposal.rejected_templates:
        if any((not note.template_id, not note.reason)):
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
    m = runtime._check_proposals
    if not isinstance(m, dict):
        runtime._check_proposals = {}
        m = runtime._check_proposals
    return m

def _reviews_map(runtime: Any) -> dict[str, dict[str, Any]]:
    try:
        m = runtime._proposed_check_reviews
    except AttributeError:
        m = None
    if m is None:
        runtime._proposed_check_reviews = {}
        m = runtime._proposed_check_reviews
    elif not isinstance(m, dict):
        raise JournalFormatError(
            f"_proposed_check_reviews must be dict, got {type(m).__name__}"
        )
    return m

def proposal_count(runtime: Any) -> int:
    """Number of CheckProposed events folded on this study (quota counter)."""
    return len(_proposals_map(runtime))

def get_proposal(runtime: Any, proposal_id_s: str) -> dict[str, dict[str, Any]] | dict[str, Any] | None:
    return optional_item(_proposals_map(runtime), str(proposal_id_s))

def proposal_is_approved(runtime: Any, proposal_id_s: str) -> bool:
    row = get_proposal(runtime, proposal_id_s)
    return row is not None and optional_item(row, "status") == "approved"

def fold_proposed_check_event(runtime: Any, ev: Any) -> None:
    """Fold CheckProposed / CheckApproved / CheckRejected / ReviewScheduled."""
    t = ev.type
    p = ev.payload
    if p is None:
        raise JournalFormatError(f"{t}: missing payload")
    if not isinstance(p, dict):
        raise JournalFormatError(
            f"{t}: payload must be dict, got {type(p).__name__}"
        )
    proposals = _proposals_map(runtime)
    reviews = _reviews_map(runtime)
    seq = ev.seq
    if t == "CheckProposed":
        _tmp = optional_item(p, "proposal_id")
        if _tmp is not None:
            pid = str(_tmp)
        else:
            pid = f"{ev.entity_key}"
        digest = _or_empty_str(optional_item(p, 'proposal_digest'))
        try:
            bound_task_id = _payload_str(p, "task_id")
            bound_target = _payload_str(p, "target")
        except KeyError as e:
            raise JournalFormatError(
                f"CheckProposed: missing {e.args[0]!r}"
            ) from e
        proposals[pid] = {
            **p,
            "proposal_id": pid,
            "status": "proposed",
            "delegated": False,
            "approved_by": None,
            "rejected_by": None,
            "reject_reason": None,
            "proposed_seq": seq,
            # Fill bound_* once from CheckProposed task_id/target; readers use bound_* only.
            "bound_task_id": bound_task_id,
            "bound_target": bound_target,
            "proposal_digest": digest,
        }
    elif t == "CheckApproved":
        _tmp = optional_item(p, "proposal_id")
        if _tmp is not None:
            pid = str(_tmp)
        else:
            pid = f"{ev.entity_key}"
        row = dict(_proposal_row(proposals, pid))
        if optional_item(row, "status") == "rejected":
            # Rejected → approved is forbidden; ignore adversarial journal order.
            return
        expected = _or_empty_str(optional_item(row, 'proposal_digest'))
        got = _or_empty_str(optional_item(p, 'proposal_digest'))
        if expected and got and expected != got:
            return
        try:
            approved_by = p["approved_by"]
            approved_source = p["source"]
            bound_task_id = _payload_str(p, "bound_task_id")
            bound_target = _payload_str(p, "bound_target")
        except KeyError as e:
            raise JournalFormatError(
                f"CheckApproved: missing {e.args[0]!r}"
            ) from e
        row.update(
            {
                "status": "approved",
                "delegated": optional_item(p, "delegated") is True,
                "approved_by": approved_by,
                "approved_source": approved_source,
                "reviewer_attempt_id": optional_item(p, "reviewer_attempt_id"),
                "hitl_request_id": optional_item(p, "hitl_request_id"),
                "bound_task_id": bound_task_id,
                "bound_target": bound_target,
            }
        )
        proposals[pid] = row
        rev = optional_item(reviews, pid)
        if isinstance(rev, dict):
            rev["status"] = "approved"
    elif t == "CheckRejected":
        _tmp = optional_item(p, "proposal_id")
        if _tmp is not None:
            pid = str(_tmp)
        else:
            pid = f"{ev.entity_key}"
        row = dict(_proposal_row(proposals, pid))
        if optional_item(row, "status") == "approved":
            return
        try:
            rejected_by = p["rejected_by"]
        except KeyError as e:
            raise JournalFormatError(
                f"CheckRejected: missing {e.args[0]!r}"
            ) from e
        row.update(
            {
                "status": "rejected",
                "delegated": optional_item(p, "delegated") is True,
                "rejected_by": rejected_by,
                "reject_reason": optional_item(p, "reason"),
                "hitl_request_id": optional_item(p, "hitl_request_id"),
            }
        )
        proposals[pid] = row
        rev = optional_item(reviews, pid)
        if isinstance(rev, dict):
            rev["status"] = "rejected"
    elif t == "ReviewScheduled" and optional_item(p, "kind") == "proposed_check_review":
        _tmp = optional_item(p, "proposal_id")
        if _tmp is not None:
            pid = str(_tmp)
        else:
            pid = f"{ev.entity_key}"
        prev = dict(_or_empty_dict(optional_item(reviews, pid)))
        # Merge so a later bind (reviewer_attempt_id) keeps deadline/status.
        merged = {**prev, **p}
        try:
            status = _payload_str(merged, "status")
            deadline = merged["deadline"]
        except KeyError as e:
            raise JournalFormatError(
                f"ReviewScheduled: missing {e.args[0]!r}"
            ) from e
        out = {
            **merged,
            "proposal_id": pid,
            "status": status,
            "deadline": deadline,
        }
        # Canonical reviewer_attempt_id from merged tip only (no or-chain).
        if "reviewer_attempt_id" in merged:
            out["reviewer_attempt_id"] = merged["reviewer_attempt_id"]
        reviews[pid] = out

def requirement_ref_for_proposal(
    proposal_id_s: str,
    *,
    target: str,
    required: bool,
) -> RequirementRef:
    """Plan criterion for ``proposed_check`` — params carry proposal_id **and** target.

    Eval compares ``params['target']`` to the proposal's bound_target (fail-closed
    if missing) so one approval cannot satisfy a criterion for a different target.
    """
    target_s = unicodedata.normalize("NFKC", str(target)).strip()
    if not target_s:
        raise ValueError("requirement_ref_for_proposal requires non-empty target")
    return RequirementRef(
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        params={"proposal_id": str(proposal_id_s), "target": target_s},
        required=required,
    )

def _task_has_result(runtime: Any, task_id: str) -> bool:
    results = runtime.results
    if results is None:
        results = {}
    if not isinstance(results, dict):
        return False
    for row in results.values():
        if not isinstance(row, dict):
            continue
        if _or_empty_str(optional_item(row, 'task_id')) == str(task_id):
            return True
    return False

def _task_has_started_attempt(
    runtime: Any,
    task_id: str,
    *,
    except_attempt_id: str | None = None,
) -> bool:
    """True when the bound task has a live AttemptStarted other than ``except``.

    The proposer's own open attempt is exempt so an in-attempt propose is still
    journaled (and then fails fail-closed prereg at eval). A foreign live
    attempt on the bound task blocks any new proposal_id.
    """
    attempts = runtime.attempts
    if attempts is None:
        attempts = {}
    except_id = _or_empty_str(except_attempt_id)
    for att in attempts.values():
        if not isinstance(att, dict):
            continue
        if _or_empty_str(optional_item(att, 'task_id')) != str(task_id):
            continue
        if optional_item(att, "closed"):
            continue
        aid = _or_empty_str(optional_item(att, 'attempt_id'))
        if except_id and aid == except_id:
            continue
        return True
    return False

def _result_seq_for_task(
    results: Mapping[str, Mapping[str, Any]] | None, task_id: str
) -> int | None:
    """Earliest ResultRecorded._seq for ``task_id``.

    Returns ``None`` when no result exists. Returns ``-1`` (fail-closed sentinel)
    when any matching row lacks a usable ``_seq`` — including mixed rows where
    some have ``_seq`` and others do not (never silently ``min`` over a subset).
    """
    if not results:
        return None
    found = False
    best: int | None = None
    missing_seq = False
    for row in results.values():
        if not isinstance(row, dict):
            continue
        if _or_empty_str(optional_item(row, 'task_id')) != str(task_id):
            continue
        found = True
        seq = optional_item(row, "_seq")
        if isinstance(seq, int):
            if best is None:
                best = seq
            else:
                best = min(best, seq)
        else:
            missing_seq = True
    if not found:
        return None
    # Fail-closed on any missing _seq among matching rows (mixed or all-missing).
    if missing_seq:
        return -1
    return best

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
    target_kind: Literal["question", "hypothesis"] | None = None,
    check: CodeCheck | Mapping[str, Any] | None = None,
    rubric: Rubric | Mapping[str, Any] | None = None,
    threshold: Any = None,
    sesoi: float | None = None,
    uses_sesoi: bool | None = None,
    predicts_null: bool | None = None,
    predicted_direction: str | None = None,
    open_hitl: bool | None = None,
    timeout_s: int | None = None,
) -> str | None:
    """Preregister a proposed check. Returns proposal_id, or None if quota closed the task.

    Writes ``CheckProposed`` (and optionally ``HitlOpened`` kind=proposed_check).
    Must run before the evaluating attempt starts and before any result for task/target.
    Exceeding ``max_proposals`` closes the proposing task with ``PROPOSAL_QUOTA``.
    """
    if target_kind is None:
        target_kind = "question"
    if uses_sesoi is None:
        uses_sesoi = False
    if predicts_null is None:
        predicts_null = False
    if predicted_direction is None:
        predicted_direction = "two_sided"
    if open_hitl is None:
        open_hitl = True
    study = runtime.study
    if study is None:
        raise ValueError("propose_check requires an open study")
    if runtime._finalized:
        raise StudyClosedError(study.study_id, "CheckProposed")
    sid = study.study_id
    max_p = study.max_proposals
    if proposal_count(runtime) >= max_p:
        await _close_task_proposal_quota(runtime, task_id=task_id, attempt_id=attempt_id)
        return None

    # Refuse while a foreign evaluating attempt is live, and block any new pid
    # once a ResultRecorded exists for the bound task (prereg must precede both).
    # New proposal ids before eval starts are also capped by max_proposals above
    # (quota close); this gate is the temporal prereg window, not a second quota.
    if _task_has_started_attempt(
        runtime, task_id, except_attempt_id=str(attempt_id)
    ):
        raise ValueError(
            f"cannot propose_check for task {task_id!r}: evaluating attempt already started"
        )
    if _task_has_result(runtime, task_id):
        raise ValueError(
            f"cannot propose_check for task {task_id!r}: result already recorded"
        )
    target_s = unicodedata.normalize("NFKC", str(target)).strip()

    notes: list[RejectedTemplateNote]
    notes = []
    for n in rejected_templates:
        if isinstance(n, RejectedTemplateNote):
            notes.append(n)
        else:
            notes.append(RejectedTemplateNote.model_validate(n))

    check_obj: CodeCheck | None = None
    if check is not None:
        if isinstance(check, CodeCheck):
            check_obj = check
        else:
            check_obj = CodeCheck.model_validate(check)
    rubric_obj: Rubric | None = None
    if rubric is not None:
        if isinstance(rubric, Rubric):
            rubric_obj = rubric
        else:
            rubric_obj = Rubric.model_validate(rubric)
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
        proposal_digest="",
    )
    validate_proposal_content(draft)
    digest = compute_proposal_digest(draft)
    proposal = draft.model_copy(update={"proposal_digest": digest})

    now = runtime.clock.now()
    events: list[UnstampedEvent] = [
        UnstampedEvent(
            study_id=sid,
            type="CheckProposed",
            entity_key=pid,
            idem_key=make_idem("CheckProposed", pid),
            payload=proposal.model_dump(mode="json"),
            ts=now,
            schema_version=CHECK_PROPOSED_SCHEMA_VERSION,
                )
    ]

    hitl_rid: str | None = None
    from coscientist.core.execution.hitl_guard import supports_hitl

    # Core runtimes reject open HITL on reopen (HitlUnsupported); only open when supported.
    if open_hitl and supports_hitl(runtime):
        hitl_rid = hitl_request_id()
        task = optional_item(runtime.tasks, task_id)
        gate = study.hitl_policy.gate("proposed_check")
        if gate is None:
            raise JournalFormatError(
                "HITL kind 'proposed_check' has no gate in hitl_policy"
            )
        if timeout_s is not None:
            to = timeout_s
        else:
            to = gate.timeout_s
        deadline = min(
            now + timedelta(seconds=to),
            _task_or_study_deadline(task, study),
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
            cost_rub=0.0,
        )
        # payload_ref is the proposal_id; do not add extra fields (HitlRequest
        # is extra=forbid — experimental runtime validates the open payload).
        events.append(
            UnstampedEvent(
                study_id=sid,
                type="HitlOpened",
                entity_key=hitl_rid,
                idem_key=make_idem("HitlOpened", hitl_rid),
                payload=req.model_dump(mode="json"),
                ts=now,
                schema_version=HITL_OPENED_SCHEMA_VERSION,
                )
        )

    await _append_events(runtime, events)
    return pid

def proposed_check_decision_lock(runtime: Any, proposal_id: str) -> asyncio.Lock:
    """Per-proposal lock shared by HITL answer/timeout, consume, and stale reject.

    Same storage key as ``HitlForkMixin._proposed_check_decision_lock`` so all
    approve/reject side effects serialize on one lock per proposal.
    """
    locks = runtime._hitl_decision_locks
    if not isinstance(locks, dict):
        locks = {}
        runtime._hitl_decision_locks = locks
    key = _or_empty_str(proposal_id)
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]

def _decision_lock_owners(runtime: Any) -> dict[str, asyncio.Task[Any]]:
    owners = runtime._hitl_decision_lock_owners
    if not isinstance(owners, dict):
        owners = {}
        runtime._hitl_decision_lock_owners = owners
    return owners

@asynccontextmanager
async def hold_proposed_check_decision_lock(
    runtime: Any, proposal_id: str
) -> AsyncIterator[None]:
    """Acquire the per-proposal lock, or nest if *this* task already holds it.

    Ownership is keyed by ``asyncio.current_task()`` (not a ContextVar) so a
    child task never inherits a false re-entry from its parent.
    """
    pid = str(proposal_id)
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("hold_proposed_check_decision_lock requires a running task")
    owners = _decision_lock_owners(runtime)
    if optional_item(owners, pid) is task:
        yield
        return
    lock = proposed_check_decision_lock(runtime, pid)
    await lock.acquire()
    owners[pid] = task
    try:
        yield
    finally:
        if optional_item(owners, pid) is task:
            del owners[pid]
        lock.release()

async def _append_events(runtime: Any, events: list[UnstampedEvent]) -> None:
    append = runtime._append
    if callable(append):
        await append(events)
        return
    await runtime.writer.append(events)
    on_events = runtime._project_local
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
    from coscientist.core.execution.domain import TaskState

    study = runtime.study
    if not (study is not None):
        raise RuntimeError('assertion failed: study is not None')
    if runtime._finalized:
        raise StudyClosedError(study.study_id, "TaskDone")
    now = runtime.clock.now()
    task = optional_item(runtime.tasks, task_id)
    events: list[UnstampedEvent]
    events = []
    if attempt_id and attempt_id in runtime.attempts:
        att = runtime.attempts[attempt_id]
        if not optional_item(att, "closed"):
            events.append(
                UnstampedEvent(
                    study_id=study.study_id,
                    type="AttemptClosed",
                    entity_key=attempt_id,
                    idem_key=make_idem("AttemptClosed", attempt_id, "proposal_quota"),
                    payload={
                        "attempt_id": attempt_id,
                        "close_reason": CloseReason.FAILED.value,
                    },
                    ts=now,
                    schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
)
            )
    if task is not None and task.state != TaskState.DONE:
        events.append(
            UnstampedEvent(
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
                schema_version=TASK_DONE_SCHEMA_VERSION,
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
    """Reviewer must exist, be live, bound on ReviewScheduled, different task/author."""
    aid = _or_empty_str(reviewer_attempt_id)
    if not aid:
        raise ValueError("delegated approval requires reviewer_attempt_id")
    attempts = runtime.attempts
    if attempts is None:
        attempts = {}
    att = optional_item(attempts, aid)
    if not isinstance(att, dict):
        raise ValueError(f"unknown reviewer_attempt_id: {aid!r}")
    try:
        proposer = _payload_str(row, "proposer_attempt_id")
        prop_task = _payload_str(row, "bound_task_id")
    except KeyError as e:
        raise JournalFormatError(
            f"CheckProposed: missing {e.args[0]!r}"
        ) from e
    if proposer and aid == proposer:
        raise ValueError("delegated reviewer must differ from the proposal author attempt")
    if optional_item(att, "closed"):
        raise AttemptClosedError(aid)
    rev_task = _or_empty_str(optional_item(att, 'task_id'))
    if prop_task and rev_task and prop_task == rev_task:
        raise ValueError("reviewer attempt must belong to a different task than the proposer")
    reviews = _reviews_map(runtime)
    pid = _or_empty_str(optional_item(row, 'proposal_id'))
    rev = optional_item(reviews, pid)
    if not isinstance(rev, dict):
        raise ValueError(f"no ReviewScheduled(proposed_check_review) for {pid!r}")
    bound = _or_empty_str(optional_item(rev, 'reviewer_attempt_id'))
    if not bound:
        raise ValueError(
            f"reviewer not bound on ReviewScheduled for {pid!r}; "
            "call bind_proposed_check_reviewer first"
        )
    if bound != aid:
        raise ValueError(
            f"reviewer_attempt_id {aid!r} not bound on ReviewScheduled "
            f"(bound={bound!r})"
        )
    return att

def _lookup_hitl_request(runtime: Any, request_id: str) -> Any | None:
    try:
        hitl_map = runtime.hitl
    except AttributeError:
        hitl_map = None
    if not isinstance(hitl_map, dict):
        try:
            hitl_map = runtime._hitl_requests
        except AttributeError:
            hitl_map = {}
        if hitl_map is None:
            hitl_map = {}
    if not isinstance(hitl_map, dict):
        return None
    return optional_item(hitl_map, request_id)

def _lookup_hitl_decision(runtime: Any, request_id: str) -> Mapping[str, Any] | None:
    decisions = runtime._hitl_decisions
    if not isinstance(decisions, dict):
        return None
    row = optional_item(decisions, request_id)
    if isinstance(row, dict):
        return row
    else:
        return None

def _verify_human_principal(
    runtime: Any,
    *,
    source: ApprovalSource,
    proposal_id_s: str,
    hitl_request_id_s: str | None,
    human_principal: str | None,
    expect_decisions: frozenset[str],
) -> str:
    """Return auditable principal id, or raise (fail-closed — no anonymous human).

    Both ``source=hitl`` and ``source=api`` require:
    1. journaled ``HitlOpened`` with ``kind=proposed_check`` + matching ``payload_ref``;
    2. journaled ``HitlDecided(by=human)`` for that rid (from ``answer_hitl``), with a
       decision in ``expect_decisions``.

    ``HitlOpened`` alone (including the one ``propose_check`` writes) never
    authenticates ``by=human``. A free-form ``human_principal`` string alone never
    authenticates either.
    """
    del human_principal  # audit-only callers may still pass it; never sufficient alone
    if source not in ("hitl", "api"):
        raise ValueError(f"human principal not applicable for source={source!r}")
    rid = _or_empty_str(hitl_request_id_s).strip()
    if not rid:
        raise ValueError(
            f"{source} approval requires hitl_request_id bound to authenticated HitlOpened"
        )
    h = _lookup_hitl_request(runtime, rid)
    if h is None:
        raise ValueError(
            f"{source} approval requires verifiable HitlOpened request: {rid!r}"
        )
    if isinstance(h, dict):
        kind = _or_empty_str(optional_item(h, 'kind'))
        ref = _or_empty_str(optional_item(h, "payload_ref"))
        if not ref:
            ref = _or_empty_str(optional_item(h, "proposal_id"))
    else:
        kind = _or_empty_str(h.kind)
        ref = _or_empty_str(h.payload_ref)
    # Fail-closed: empty kind/ref are not a free pass (must equal expected).
    if kind != "proposed_check":
        raise ValueError(
            f"hitl request kind must be proposed_check, got {kind!r}"
        )
    if ref != str(proposal_id_s):
        raise ValueError(
            f"hitl request {rid!r} not bound to proposal {proposal_id_s!r} "
            f"(payload_ref={ref!r})"
        )
    # Propose-written HitlOpened is not enough — need answer_hitl's HitlDecided.
    dec = _lookup_hitl_decision(runtime, rid)
    if dec is None:
        raise ValueError(
            f"{source} approval requires journaled HitlDecided(by=human) for {rid!r} "
            "(HitlOpened from propose_check alone is not a human decision)"
        )
    try:
        by = _payload_str(dec, "by").strip()
    except KeyError as e:
        raise JournalFormatError("HitlDecided: missing 'by'") from e
    if by != "human":
        raise ValueError(
            f"HitlDecided for {rid!r} must be by=human, got {by!r}"
        )
    decision = _or_empty_str(optional_item(dec, 'decision')).strip()
    if decision not in expect_decisions:
        raise ValueError(
            f"HitlDecided for {rid!r} decision={decision!r} not in {sorted(expect_decisions)}"
        )
    dec_pid = _or_empty_str(optional_item(dec, 'proposal_id')).strip()
    # Strict: missing/empty proposal_id on HitlDecided is not a free pass.
    if dec_pid != str(proposal_id_s):
        raise ValueError(
            f"HitlDecided for {rid!r} bound to proposal {dec_pid!r}, not {proposal_id_s!r}"
        )
    return f"hitl:{rid}"

async def bind_proposed_check_reviewer(
    runtime: Any,
    proposal_id_s: str,
    reviewer_attempt_id: str,
) -> None:
    """Journal ReviewScheduled bind — only the designated reviewer attempt.

    Callers cannot choose ``attempt_role``; ``start_attempt`` derives
    ``attempt_role=reviewer`` from ``ReviewScheduled.reviewer_task_id``. Bind
    requires that role, a matching designated task, and a pending review row.
    """
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    if optional_item(row, "status") != "proposed":
        raise ValueError(f"proposal not pending review: {proposal_id_s!r}")
    reviews = _reviews_map(runtime)
    rev = optional_item(reviews, str(proposal_id_s))
    if not isinstance(rev, dict):
        raise ValueError(
            f"no ReviewScheduled(proposed_check_review) for {proposal_id_s!r}"
        )
    try:
        rev_status = _payload_str(rev, "status")
    except KeyError as e:
        raise JournalFormatError("ReviewScheduled: missing 'status'") from e
    if rev_status != "pending":
        raise ValueError(f"review not pending for {proposal_id_s!r}")
    designated = _or_empty_str(optional_item(rev, 'reviewer_task_id')).strip()
    if not designated:
        raise ValueError(
            f"ReviewScheduled for {proposal_id_s!r} missing reviewer_task_id"
        )
    existing = _or_empty_str(optional_item(rev, 'reviewer_attempt_id'))
    aid = _or_empty_str(reviewer_attempt_id)
    if existing and existing != aid:
        raise ValueError(
            f"ReviewScheduled already bound to reviewer {existing!r}"
        )
    if existing == aid:
        return
    attempts = runtime.attempts
    if attempts is None:
        attempts = {}
    att = optional_item(attempts, aid)
    if not isinstance(att, dict):
        raise ValueError(f"unknown reviewer_attempt_id: {aid!r}")
    try:
        proposer = _payload_str(row, "proposer_attempt_id")
    except KeyError as e:
        raise JournalFormatError(
            "CheckProposed: missing 'proposer_attempt_id'"
        ) from e
    if proposer and aid == proposer:
        raise ValueError("delegated reviewer must differ from the proposal author attempt")
    if optional_item(att, "closed"):
        raise AttemptClosedError(aid)
    try:
        role = _payload_str(att, "attempt_role")
    except KeyError as e:
        raise InvalidStatus("attempt_role required on attempt") from e
    if role not in ("agent", "reviewer"):
        raise InvalidStatus(f"unknown attempt_role: {role!r}")
    if role != "reviewer":
        raise ValueError(
            f"bind requires attempt_role=reviewer from journal, got {role!r}"
        )
    try:
        prop_task = _payload_str(row, "bound_task_id")
    except KeyError as e:
        raise JournalFormatError(
            "CheckProposed: missing 'bound_task_id'"
        ) from e
    rev_task = _or_empty_str(optional_item(att, 'task_id'))
    if prop_task and rev_task and prop_task == rev_task:
        raise ValueError("reviewer attempt must belong to a different task than the proposer")
    if rev_task != designated:
        raise ValueError(
            f"reviewer attempt task {rev_task!r} != ReviewScheduled.reviewer_task_id "
            f"{designated!r}"
        )
    study = runtime.study
    if not (study is not None):
        raise RuntimeError('assertion failed: study is not None')
    now = runtime.clock.now()
    try:
        rev_task_id = rev["task_id"]
        rev_deadline = rev["deadline"]
    except KeyError as e:
        raise JournalFormatError(
            f"ReviewScheduled: missing {e.args[0]!r}"
        ) from e
    payload = {
        **{k: v for k, v in rev.items() if k != "status"},
        "kind": "proposed_check_review",
        "proposal_id": str(proposal_id_s),
        "reviewer_attempt_id": aid,
        "reviewer_task_id": designated,
        "status": "pending",
        "deadline": rev_deadline,
        "hitl_request_id": optional_item(rev, "hitl_request_id"),
        "task_id": rev_task_id,
        "proposer_attempt_id": proposer,
    }
    await _append_events(
        runtime,
        [
            UnstampedEvent(
                study_id=study.study_id,
                type="ReviewScheduled",
                entity_key=str(proposal_id_s),
                idem_key=make_idem(
                    "ReviewScheduled", str(proposal_id_s), "proposed_check", "bind", aid
                ),
                payload=payload,
                ts=now,
                schema_version=REVIEW_SCHEDULED_SCHEMA_VERSION,
                )
        ],
    )

async def approve_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    source: ApprovalSource,
    delegated: bool,
    reviewer_attempt_id: str | None = None,
    hitl_request_id_s: str | None = None,
    human_principal: str | None = None,
) -> None:
    """Write CheckApproved. ``source`` is required — ``human`` only via hitl/api."""
    if source not in ("hitl", "api", "reviewer"):
        raise ValueError(f"invalid approval source: {source!r}")
    if delegated and source != "reviewer":
        raise ValueError("delegated approval requires source='reviewer'")
    if not delegated and source == "reviewer":
        raise ValueError("source='reviewer' requires delegated=True")
    async with hold_proposed_check_decision_lock(runtime, proposal_id_s):
        row = get_proposal(runtime, proposal_id_s)
        if row is None:
            raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
        if optional_item(row, "status") == "approved":
            return
        # External API/reviewer/hitl: fail-closed if already rejected (lost race
        # is not silent success). Timeout/stale never call approve_check.
        if optional_item(row, "status") == "rejected":
            raise ValueError(f"proposal already rejected: {proposal_id_s!r}")

        by: Literal["human", "reviewer"]
        principal: str | None = None
        if source in ("hitl", "api"):
            by = "human"
            delegated = False
            reviewer_attempt_id = None
            principal = _verify_human_principal(
                runtime,
                source=source,
                proposal_id_s=str(proposal_id_s),
                hitl_request_id_s=hitl_request_id_s,
                human_principal=human_principal,
                expect_decisions=frozenset({"accept", "approve"}),
            )
        else:
            by = "reviewer"
            _validate_reviewer_attempt(
                runtime, row=row, reviewer_attempt_id=_or_empty_str(reviewer_attempt_id)
            )

        study = runtime.study
        if not (study is not None):
            raise RuntimeError('assertion failed: study is not None')
        now = runtime.clock.now()
        try:
            bound_task_id = _payload_str(row, "bound_task_id")
            bound_target = _payload_str(row, "bound_target")
        except KeyError as e:
            raise JournalFormatError(
                f"CheckApproved: missing {e.args[0]!r} on proposal"
            ) from e
        if any((not bound_task_id, not bound_target)):
            raise JournalFormatError(
                "approval requires bound_task_id and bound_target on proposal"
            )
        payload = {
            "proposal_id": str(proposal_id_s),
            "proposal_digest": optional_item(row, "proposal_digest"),
            "by": by,
            "approved_by": by,
            "source": source,
            "delegated": delegated is True,
            "reviewer_attempt_id": reviewer_attempt_id,
            "hitl_request_id": hitl_request_id_s,
            "human_principal": principal,
            "bound_task_id": bound_task_id,
            "bound_target": bound_target,
        }
        # Idem key = proposal id only (not payload hash) so retries are stable.
        ev = UnstampedEvent(
            study_id=study.study_id,
            type="CheckApproved",
            entity_key=str(proposal_id_s),
            idem_key=make_idem("CheckApproved", str(proposal_id_s)),
            payload=payload,
            ts=now,
            schema_version=CHECK_APPROVED_SCHEMA_VERSION,
                )
        # HitlDecided(by=human) must already be journaled (answer_hitl); do not mint it
        # here — that would let source=api|hitl approve from propose_check's HitlOpened alone.
        await _append_events(runtime, [ev])

async def reject_check(
    runtime: Any,
    proposal_id_s: str,
    *,
    source: ApprovalSource | Literal["timeout"],  # type: ignore[assignment]
    reason: str,
    delegated: bool,
    hitl_request_id_s: str | None = None,
    reviewer_attempt_id: str | None = None,
    human_principal: str | None = None,
) -> None:
    """Write CheckRejected (HITL timeout / human reject / reviewer reject)."""
    async with hold_proposed_check_decision_lock(runtime, proposal_id_s):
        row = get_proposal(runtime, proposal_id_s)
        if row is None:
            raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
        if optional_item(row, "status") == "rejected":
            return
        if optional_item(row, "status") == "approved":
            # Internal race paths (HITL timeout / stale review) no-op; external
            # api/hitl/reviewer must fail-closed — never silent success.
            if source == "timeout":
                return
            raise ValueError(f"proposal already approved: {proposal_id_s!r}")
        # Race: HitlDecided(accept) already journaled but CheckApproved not yet —
        # timeout must not clobber the in-flight human accept.
        if source == "timeout" and hitl_request_id_s:
            dec = _lookup_hitl_decision(runtime, str(hitl_request_id_s))
            if isinstance(dec, dict):
                d = _or_empty_str(optional_item(dec, 'decision')).strip()
                decided_by = _payload_str(dec, "by").strip()
                if decided_by == "human" and d in ("accept", "approve"):
                    return
        principal: str | None = None
        if source == "reviewer":
            _validate_reviewer_attempt(
                runtime, row=row, reviewer_attempt_id=_or_empty_str(reviewer_attempt_id)
            )
        elif source in ("hitl", "api"):
            principal = _verify_human_principal(
                runtime,
                source=source,  # type: ignore[arg-type]
                proposal_id_s=str(proposal_id_s),
                hitl_request_id_s=hitl_request_id_s,
                human_principal=human_principal,
                expect_decisions=frozenset({"reject"}),
            )
        study = runtime.study
        if not (study is not None):
            raise RuntimeError('assertion failed: study is not None')
        now = runtime.clock.now()
        by: str
        if source == "timeout":
            by = "timeout"
        elif source in ("hitl", "api"):
            by = "human"
        else:
            by = "reviewer"
        payload = {
            "proposal_id": str(proposal_id_s),
            "proposal_digest": optional_item(row, "proposal_digest"),
            "by": by,
            "rejected_by": by,
            "source": source,
            "reason": str(reason),
            "delegated": delegated is True,
            "hitl_request_id": hitl_request_id_s,
            "reviewer_attempt_id": reviewer_attempt_id,
            "human_principal": principal,
        }
        events = [
            UnstampedEvent(
                study_id=study.study_id,
                type="CheckRejected",
                entity_key=str(proposal_id_s),
                idem_key=make_idem("CheckRejected", str(proposal_id_s)),
                payload=payload,
                ts=now,
                schema_version=CHECK_REJECTED_SCHEMA_VERSION,
                )
        ]
        if hitl_request_id_s and reason == "human_timeout":
            events.append(
                UnstampedEvent(
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
                    schema_version=writer_schema_version('HitlTimedOut'),
)
            )
        # Human reject: HitlDecided(by=human, decision=reject) must already be journaled
        # (answer_hitl); do not mint a decision here.
        await _append_events(runtime, events)

async def schedule_proposed_check_review(
    runtime: Any,
    proposal_id_s: str,
    *,
    reviewer_task_id: str | None = None,
    hitl_request_id_s: str | None = None,
    timeout_s: int | None = None,
) -> None:
    """Journal ReviewScheduled(proposed_check_review) with a hard deadline (timeout≡reject).

    ``reviewer_task_id`` designates which task may start a ``attempt_role=reviewer``
    attempt and call ``bind_proposed_check_reviewer`` (required before bind).
    """
    row = get_proposal(runtime, proposal_id_s)
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
    study = runtime.study
    if not (study is not None):
        raise RuntimeError('assertion failed: study is not None')
    now = runtime.clock.now()
    gate = study.hitl_policy.gate("proposed_check")
    if gate is None:
        raise JournalFormatError(
            "HITL kind 'proposed_check' has no gate in hitl_policy"
        )
    if timeout_s is not None:
        to = timeout_s
    else:
        to = gate.timeout_s
    try:
        prop_task_id = _payload_str(row, "task_id")
        proposer_attempt_id = row["proposer_attempt_id"]
    except KeyError as e:
        raise JournalFormatError(
            f"CheckProposed: missing {e.args[0]!r}"
        ) from e
    if not prop_task_id:
        raise JournalFormatError("CheckProposed: empty 'task_id'")
    task = optional_item(runtime.tasks, prop_task_id)
    deadline = min(
        now + timedelta(seconds=to),
        _task_or_study_deadline(task, study),
        study.effective_deadline(),
    )
    if reviewer_task_id:
        designated = _or_empty_str(reviewer_task_id).strip()
    else:
        designated = ""
    if designated:
        tasks_bag = runtime.tasks
        if designated not in tasks_bag:
            raise ValueError(f"unknown reviewer_task_id: {designated!r}")
        prop_task = _payload_str(row, "bound_task_id")
        if prop_task and designated == prop_task:
            raise ValueError("reviewer_task_id must differ from the proposer task")
    await _append_events(
        runtime,
        [
            UnstampedEvent(
                study_id=study.study_id,
                type="ReviewScheduled",
                entity_key=str(proposal_id_s),
                idem_key=make_idem("ReviewScheduled", str(proposal_id_s), "proposed_check"),
                payload={
                    "kind": "proposed_check_review",
                    "proposal_id": str(proposal_id_s),
                    "hitl_request_id": hitl_request_id_s,
                    "task_id": prop_task_id,
                    "reviewer_task_id": designated or None,
                    "proposer_attempt_id": proposer_attempt_id,
                    "deadline": deadline.isoformat(),
                    "status": "pending",
                },
                ts=now,
                schema_version=REVIEW_SCHEDULED_SCHEMA_VERSION,
)
        ],
    )

async def consume_proposed_check_review(
    runtime: Any,
    proposal_id_s: str,
    *,
    decision: Literal["approve", "reject"],
    reviewer_attempt_id: str,
    reason: str,
) -> None:
    """Consumer of ReviewScheduled(kind=proposed_check_review): reviewer decides.

    Bind runs before the lock (needs a pending proposal). Under the lock the
    status is re-checked fail-closed: a concurrent timeout/stale reject between
    bind and acquire must raise (never silent success for the external reviewer).
    """
    await bind_proposed_check_reviewer(runtime, proposal_id_s, reviewer_attempt_id)
    async with hold_proposed_check_decision_lock(runtime, proposal_id_s):
        row = get_proposal(runtime, proposal_id_s)
        if row is None:
            raise ValueError(f"unknown proposal_id: {proposal_id_s!r}")
        status = optional_item(row, "status")
        if status != "proposed":
            # External reviewer path — same fail-closed contract as approve/reject.
            if status == "rejected":
                raise ValueError(f"proposal already rejected: {proposal_id_s!r}")
            if status == "approved":
                raise ValueError(f"proposal already approved: {proposal_id_s!r}")
            raise ValueError(
                f"proposal not pending review: {proposal_id_s!r} status={status!r}"
            )
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
    rejected: list[str]
    rejected = []
    for pid, rev in list(_reviews_map(runtime).items()):
        if _payload_str(rev, "status") != "pending":
            continue
        row = get_proposal(runtime, pid)
        if any((row is None, optional_item(row, 'status') != 'proposed')):
            continue
        dl_raw = optional_item(rev, "deadline")
        if not dl_raw:
            continue
        from datetime import datetime

        if isinstance(dl_raw, datetime):
            dl = dl_raw
        else:
            dl = datetime.fromisoformat(str(dl_raw))
        if now <= dl:
            continue
        # Same per-proposal lock as HITL / consume — re-check under the lock so a
        # concurrent reviewer approve_check cannot race CheckRejected.
        async with hold_proposed_check_decision_lock(runtime, pid):
            rev_live = optional_item(_reviews_map(runtime), pid)
            if not isinstance(rev_live, dict):
                continue
            if _payload_str(rev_live, "status") != "pending":
                continue
            row_live = get_proposal(runtime, pid)
            if any((row_live is None, optional_item(row_live, 'status') != 'proposed')):
                continue
            dl_raw2 = optional_item(rev_live, "deadline")
            if not dl_raw2:
                continue
            if isinstance(dl_raw2, datetime):
                dl2 = dl_raw2
            else:
                dl2 = datetime.fromisoformat(str(dl_raw2))
            if now <= dl2:
                continue
            await reject_check(
                runtime,
                pid,
                source="timeout",
                reason="human_timeout",
                delegated=True,
                hitl_request_id_s=_or_empty_str(optional_item(rev_live, 'hitl_request_id')) or None,
            )
            rejected.append(pid)
    return rejected

def assert_proposal_preregistered_for_eval(
    row: Mapping[str, Any],
    *,
    attempt_id: str | None,
    task_id: str | None,
    target: str | None,
    attempts: Mapping[str, Mapping[str, Any]] | None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> str | None:
    """Return exclusion detail if prereg/bind invariants fail; else None.

    Fail-closed: missing ``attempts``, ``proposed_seq``, or AttemptStarted ``_seq``
    is an exclusion (never a silent pass). CheckProposed must precede the
    evaluating AttemptStarted and any ResultRecorded for the bound task; approval
    is bound to one task_id + target so one approval cannot feed several claims.
    """
    try:
        bound_task = _payload_str(row, "bound_task_id")
        bound_target = _payload_str(row, "bound_target")
    except KeyError as e:
        if e.args:
            missing = _or_empty_str(e.args[0])
        else:
            missing = _MISSING_BOUND
        missing_err = f"unapproved_check:missing_{missing}"
    else:
        missing_err = None
    if missing_err is not None:
        return missing_err
    if not bound_task:
        return "unapproved_check:missing_bound_task"
    if not bound_target.strip():
        return "unapproved_check:missing_bound_target"
    if any((task_id is None, str(task_id) == '')):
        return "unapproved_check:missing_task_id"
    if str(task_id) != bound_task:
        return f"unapproved_check:task_mismatch:{bound_task}!={task_id}"
    # Fail-closed: eval target must come from RequirementRef.params (not a silent
    # fallback to bound_target — that would let one approval cover other targets).
    if any((target is None, not str(target).strip())):
        return "unapproved_check:missing_eval_target"
    want = unicodedata.normalize("NFKC", str(target)).strip()
    got = unicodedata.normalize("NFKC", bound_target).strip()
    if want != got:
        return f"unapproved_check:target_mismatch:{got!r}!={want!r}"

    proposed_seq = optional_item(row, "proposed_seq")
    if not isinstance(proposed_seq, int):
        return "unapproved_check:missing_proposed_seq"
    if attempts is None:
        return "unapproved_check:missing_attempts"
    if not attempt_id:
        return "unapproved_check:missing_attempt_id"
    att = optional_item(attempts, str(attempt_id))
    if not isinstance(att, dict):
        return f"unapproved_check:unknown_attempt:{attempt_id}"
    start_seq = optional_item(att, "_seq")
    if not isinstance(start_seq, int):
        return "unapproved_check:missing_attempt_seq"
    if proposed_seq >= start_seq:
        return (
            "unapproved_check:prereg_after_attempt:"
            f"proposed_seq={proposed_seq}>=attempt_seq={start_seq}"
        )

    result_seq = _result_seq_for_task(results, bound_task)
    if result_seq == -1:
        return "unapproved_check:missing_result_seq"
    if isinstance(result_seq, int) and proposed_seq >= result_seq:
        return (
            "unapproved_check:prereg_after_result:"
            f"proposed_seq={proposed_seq}>=result_seq={result_seq}"
        )
    return None

def code_check_from_proposal(row: Mapping[str, Any]) -> CodeCheck | None:
    """Build the frozen CodeCheck from a folded proposal row (None if rubric)."""
    raw = optional_item(row, "check")
    if raw is None:
        return None
    if isinstance(raw, CodeCheck):
        return raw
    else:
        return CodeCheck.model_validate(raw)

def rubric_from_proposal(row: Mapping[str, Any]) -> Rubric | None:
    raw = optional_item(row, "rubric")
    if raw is None:
        return None
    if isinstance(raw, Rubric):
        r = raw
    else:
        r = Rubric.model_validate(raw)
    return freeze_rubric(r)

def custom_checks_report(runtime: Any) -> list[dict[str, Any]]:
    """Report section ``custom_checks[]`` (non-catalog; DESIGN_M1 §16)."""
    out: list[dict[str, Any]]
    out = []
    for pid, row in sorted(_proposals_map(runtime).items()):
        out.append(
            {
                "proposal_id": pid,
                "proposal_digest": optional_item(row, "proposal_digest"),
                "target": optional_item(row, "target"),
                "target_kind": optional_item(row, "target_kind"),
                "role": optional_item(row, "role"),
                "measures": optional_item(row, "measures"),
                "status": optional_item(row, "status"),
                "delegated": optional_item(row, "delegated") is True,
                "approved_by": optional_item(row, "approved_by"),
                "approved_source": optional_item(row, "approved_source"),
                "rejected_by": optional_item(row, "rejected_by"),
                "reject_reason": optional_item(row, "reject_reason"),
                "justification": optional_item(row, "justification"),
                "rejected_templates": _or_empty_list(optional_item(row, "rejected_templates")),
                "bound_task_id": row["bound_task_id"],
                "bound_target": row["bound_target"],
                "catalog": False,
                "note": "не каталожная",
            }
        )
    return out

def is_delegated_proposed_claim(claim: Any) -> bool:
    """True when claim evidence comes from an approved delegated proposed_check."""
    effect = claim.effect
    if not isinstance(effect, dict):
        return False
    return optional_item(effect, "proposed_check") is True and optional_item(effect, "delegated") is True

def is_proposed_claim(claim: Any) -> bool:
    effect = claim.effect
    if not isinstance(effect, dict):
        return False
    return optional_item(effect, "proposed_check") is True
