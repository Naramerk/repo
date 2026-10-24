"""Judge / ReviewCheck apply path (M1 PR13).

Review state is journaled (``ResultPending`` / ``ReviewScheduled`` /
``ReviewRecorded``) and rebuilt on open/replay so live == restart.

Keys are scoped: ``(task_id, attempt_id, check_id, item_id, output_digest)``
encoded as a JSON array (not ``"|"``-joined) so a MET from another
attempt/task never applies.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.execution.domain import AgentTaskOutput, ReqStatus

JudgeFailureKind = Literal["timeout", "invalid_json", "refusal"]
# Keep in sync with checks.kinds.rubric.JUDGE_FAILURE_KINDS (no circular import).
JUDGE_FAILURE_KINDS = frozenset({"timeout", "invalid_json", "refusal"})

JUDGE_CAS_CORRUPT_LINE = "не выполнено: внутренняя ошибка (judge CAS)"

class JudgePendingCorrupt(Exception):
    """Pending agent output CAS missing/corrupt on rebuild — fail closed."""

def review_key(
    *,
    task_id: str,
    attempt_id: str,
    check_id: str,
    item_id: str,
    output_digest: str,
) -> str:
    """Stable scoped key for review_statuses / ReviewScheduled / record_review.

    Encoded as a JSON array so field values may contain any character.
    Empty ``task_id`` / ``attempt_id`` / ``output_digest`` is rejected (fail-closed).
    """
    if not task_id or not attempt_id or not output_digest:
        raise ValueError("review_key requires non-empty task_id, attempt_id, output_digest")
    return json.dumps(
        [str(task_id), str(attempt_id), str(check_id), str(item_id), str(output_digest)],
        ensure_ascii=False,
        separators=(",", ":"),
    )

def parse_review_key(key: str) -> dict[str, str] | None:
    try:
        parts = json.loads(key)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parts, list) or len(parts) != 5:
        return None
    return {
        "task_id": str(parts[0]),
        "attempt_id": str(parts[1]),
        "check_id": str(parts[2]),
        "item_id": str(parts[3]),
        "output_digest": str(parts[4]),
    }

def plan_refused_idem(study_id: str, detail: str) -> str:
    """Idem key for PlanRefused: sid + sha256(**full** detail)."""
    return make_idem("PlanRefused", study_id, sha256_hex(detail.encode("utf-8")))

def resolve_review_status(
    *,
    status: ReqStatus | str | None,
    failure_kind: str | None,
    prior_retries: int,
) -> tuple[ReqStatus, int, str | None]:
    """Map a review answer to status + updated retry count.

    - ``failure_kind`` in timeout|invalid_json|refusal → UNASSESSED, retry += 1
    - explicit FAILED (not-met) → FAILED (never remapped)
    - MET / WAIVED / … → as given
    """
    if failure_kind is not None and failure_kind in JUDGE_FAILURE_KINDS:
        return ReqStatus.UNASSESSED, prior_retries + 1, failure_kind
    if status is None:
        raise ValueError("record_review requires status or failure_kind")
    st = status if isinstance(status, ReqStatus) else ReqStatus(str(status))
    return st, prior_retries, None

def review_scheduled_events(
    *,
    study_id: str,
    attempt_id: str,
    task_id: str,
    output_digest: str,
    checks: list[Any],
    now: Any,
) -> list[EventEnvelope]:
    """Emit ReviewScheduled per pending/unassessed judge item (and whole ReviewChecks)."""
    events: list[EventEnvelope] = []
    seen: set[str] = set()
    for c in checks:
        effect = getattr(c, "effect", None) or {}
        item_statuses = effect.get("item_statuses") if isinstance(effect, dict) else None
        if isinstance(item_statuses, dict):
            pending = set(effect.get("pending_items") or [])
            for item_id, st in item_statuses.items():
                if (
                    st
                    not in (
                        ReqStatus.PENDING.value,
                        ReqStatus.UNASSESSED.value,
                        "PENDING",
                        "UNASSESSED",
                    )
                    and item_id not in pending
                ):
                    continue
                key = review_key(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    check_id=str(c.check_id),
                    item_id=str(item_id),
                    output_digest=output_digest,
                )
                if key in seen:
                    continue
                seen.add(key)
                events.append(
                    EventEnvelope(
                        study_id=study_id,
                        type="ReviewScheduled",
                        entity_key=attempt_id,
                        idem_key=make_idem("ReviewScheduled", attempt_id, key),
                        payload={
                            "review_key": key,
                            "check_id": str(c.check_id),
                            "item_id": str(item_id),
                            "attempt_id": attempt_id,
                            "task_id": task_id,
                            "output_digest": output_digest,
                            "kind": "rubric_judge",
                            "rubric_digest": effect.get("rubric_digest"),
                        },
                        ts=now,
                    )
                )
            continue
        if not c.is_code and getattr(c, "status", None) in (
            ReqStatus.UNASSESSED,
            ReqStatus.PENDING,
        ):
            key = review_key(
                task_id=task_id,
                attempt_id=attempt_id,
                check_id=str(c.check_id),
                item_id="",
                output_digest=output_digest,
            )
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventEnvelope(
                    study_id=study_id,
                    type="ReviewScheduled",
                    entity_key=attempt_id,
                    idem_key=make_idem("ReviewScheduled", attempt_id, key),
                    payload={
                        "review_key": key,
                        "check_id": str(c.check_id),
                        "item_id": "",
                        "attempt_id": attempt_id,
                        "task_id": task_id,
                        "output_digest": output_digest,
                        "kind": "review_check",
                    },
                    ts=now,
                )
            )
    return events

def _load_agent_output(artifacts: Any, digest: str) -> AgentTaskOutput:
    """Load pending AgentTaskOutput from CAS; raise on missing/corrupt (fail-closed)."""
    from pydantic import ValidationError

    from coscientist.core.events.errors import ArtifactCorrupt

    if artifacts is None or not digest:
        raise JudgePendingCorrupt("missing artifacts store or output digest")
    try:
        raw = artifacts.read_cas(digest)
    except (OSError, KeyError, ArtifactCorrupt) as e:
        # Narrow: expected CAS I/O / integrity — never bare Exception (bugs propagate).
        raise JudgePendingCorrupt(f"cas_read:{type(e).__name__}:{e}") from e
    try:
        return AgentTaskOutput.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError) as e:
        raise JudgePendingCorrupt(f"cas_validate:{type(e).__name__}:{e}") from e

def rebuild_judge_state(
    events: list[Any],
    *,
    artifacts: Any,
) -> tuple[
    dict[str, ReqStatus],
    dict[str, int],
    dict[str, str],
    dict[str, AgentTaskOutput],
    dict[str, str],
]:
    """Rebuild review_statuses / judge_retry / failure_kinds / pending / digests.

    Only judge-path events: ``ResultPending``, ``ReviewRecorded``,
    ``ResultRecorded``, ``AttemptClosed``. ``RouteReturned`` is ignored.
    """
    review_statuses: dict[str, ReqStatus] = {}
    judge_retry: dict[str, int] = {}
    failure_kinds: dict[str, str] = {}
    pending_digests: dict[str, str] = {}
    closed: set[str] = set()

    for ev in events:
        if not isinstance(ev, EventEnvelope):
            raise TypeError(
                f"rebuild_judge_state requires EventEnvelope, got {type(ev).__name__}"
            )
        t = ev.type
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if t == "ResultPending":
            aid = str(p.get("attempt_id") or ev.entity_key or "")
            dig = str(p.get("agent_output_digest") or "")
            if aid and dig:
                pending_digests[aid] = dig
        elif t == "ReviewRecorded":
            key = str(p.get("review_key") or "")
            if not key:
                continue
            st_raw = p.get("status")
            if st_raw is not None:
                try:
                    review_statuses[key] = st_raw if isinstance(st_raw, ReqStatus) else ReqStatus(str(st_raw))
                except ValueError:
                    pass
            retry = p.get("retry")
            if retry is not None:
                try:
                    judge_retry[key] = int(retry)
                except (TypeError, ValueError) as e:
                    raise JudgePendingCorrupt(f"retry_corrupt:{key}:{retry!r}") from e
            fk = p.get("failure_kind")
            if fk:
                failure_kinds[key] = str(fk)
            else:
                failure_kinds.pop(key, None)
        elif t in ("ResultRecorded", "AttemptClosed"):
            aid = str(p.get("attempt_id") or ev.entity_key or "")
            if aid:
                closed.add(aid)
                pending_digests.pop(aid, None)

    pending_outputs: dict[str, AgentTaskOutput] = {}
    digests: dict[str, str] = {}
    for aid, dig in pending_digests.items():
        if aid in closed:
            continue
        digests[aid] = dig
        pending_outputs[aid] = _load_agent_output(artifacts, dig)
    return review_statuses, judge_retry, failure_kinds, pending_outputs, digests

def apply_judge_journal(executor: Any, events: list[Any]) -> None:
    """Load journaled judge state onto a live Executor (open_runtime / reopen).

    Raises ``JudgePendingCorrupt`` when a pending digest cannot be loaded —
    callers must finalize the study as an honest internal error (no hang).
    """
    statuses, retries, kinds, pending, digests = rebuild_judge_state(
        events, artifacts=getattr(executor, "artifacts", None)
    )
    executor.review_statuses = statuses
    executor.judge_retry = retries
    executor.failure_kinds = kinds
    executor._pending_outputs = pending  # noqa: SLF001
    executor._output_digests = digests  # noqa: SLF001
