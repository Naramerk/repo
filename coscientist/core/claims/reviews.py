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

from coscientist.core.events.digest import sha256_hex, payload_digest
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import AgentTaskOutput, ReqStatus

JudgeFailureKind = Literal["timeout", "invalid_json", "refusal"]
# Keep in sync with checks.kinds.rubric.JUDGE_FAILURE_KINDS (no circular import).
JUDGE_FAILURE_KINDS = frozenset({"timeout", "invalid_json", "refusal"})

JUDGE_CAS_CORRUPT_LINE = "не выполнено: внутренняя ошибка (judge CAS)"

class JudgePendingCorrupt(Exception):
    """Pending agent output CAS missing/corrupt on rebuild — fail closed."""


def _require_nonempty_str(payload: dict[str, Any], key: str, *, event_type: str) -> str:
    """Require ``payload[key]`` to be a non-empty ``str`` (reject ``None`` before ``str()``)."""
    if key not in payload:
        raise JournalFormatError(f"{event_type}: missing {key!r}")
    raw = payload[key]
    if not isinstance(raw, str):
        raise JournalFormatError(
            f"{event_type}: {key} must be str, got {type(raw).__name__}"
        )
    if not raw:
        raise JournalFormatError(f"{event_type}: {key} must be non-empty")
    return raw


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
        effect = c.effect
        if effect is None:
            item_statuses = None
            pending: set[Any] = set()
        elif not isinstance(effect, dict):
            raise JournalFormatError(
                f"ReviewCheck.effect must be dict|None, got {type(effect).__name__}"
            )
        else:
            item_statuses = effect.get("item_statuses")
            if "pending_items" not in effect:
                pending = set()
            else:
                raw_pending = effect["pending_items"]
                if not isinstance(raw_pending, list):
                    raise JournalFormatError(
                        f"ReviewCheck.effect.pending_items must be list, "
                        f"got {type(raw_pending).__name__}"
                    )
                pending = set(raw_pending)
        if item_statuses is not None and not isinstance(item_statuses, dict):
            raise JournalFormatError(
                f"ReviewCheck.effect.item_statuses must be dict, "
                f"got {type(item_statuses).__name__}"
            )
        if isinstance(item_statuses, dict):
            # Sole pending token is PENDING (UNASSESSED is a different status).
            for item_id, st in item_statuses.items():
                st_s = st.value if isinstance(st, ReqStatus) else st
                if st_s != ReqStatus.PENDING.value and item_id not in pending:
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
                            "rubric_digest": (
                                effect["rubric_digest"]
                                if isinstance(effect, dict) and "rubric_digest" in effect
                                else None
                            ),
                        },
                        ts=now,
                        schema_version=writer_schema_version('ReviewScheduled'),
epoch=0, payload_digest=payload_digest({
                            "review_key": key,
                            "check_id": str(c.check_id),
                            "item_id": str(item_id),
                            "attempt_id": attempt_id,
                            "task_id": task_id,
                            "output_digest": output_digest,
                            "kind": "rubric_judge",
                            "rubric_digest": (
                                effect["rubric_digest"]
                                if isinstance(effect, dict) and "rubric_digest" in effect
                                else None
                            ),
                        }),
)
                )
            continue
        if not c.is_code and c.status in (
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
                    schema_version=writer_schema_version('ReviewScheduled'),
epoch=0, payload_digest=payload_digest({
                        "review_key": key,
                        "check_id": str(c.check_id),
                        "item_id": "",
                        "attempt_id": attempt_id,
                        "task_id": task_id,
                        "output_digest": output_digest,
                        "kind": "review_check",
                    }),
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
        if not isinstance(ev.payload, dict):
            raise JournalFormatError(
                f"{t}: payload must be dict, got {type(ev.payload).__name__}"
            )
        p = ev.payload
        if t == "ResultPending":
            aid = _require_nonempty_str(p, "attempt_id", event_type=t)
            dig = _require_nonempty_str(p, "agent_output_digest", event_type=t)
            pending_digests[aid] = dig
        elif t == "ReviewRecorded":
            key = _require_nonempty_str(p, "review_key", event_type=t)
            st_raw = p.get("status")
            if st_raw is not None:
                if isinstance(st_raw, ReqStatus):
                    review_statuses[key] = st_raw
                elif isinstance(st_raw, str):
                    try:
                        review_statuses[key] = ReqStatus(st_raw)
                    except ValueError as e:
                        raise JournalFormatError(
                            f"ReviewRecorded: invalid status={st_raw!r}"
                        ) from e
                else:
                    raise JournalFormatError(
                        f"ReviewRecorded: status must be str|ReqStatus, "
                        f"got {type(st_raw).__name__}"
                    )
            retry = p.get("retry")
            if retry is not None:
                try:
                    judge_retry[key] = int(retry)
                except (TypeError, ValueError) as e:
                    raise JudgePendingCorrupt(f"retry_corrupt:{key}:{retry!r}") from e
            fk = p.get("failure_kind")
            if fk is not None:
                if not isinstance(fk, str) or not fk:
                    raise JournalFormatError(
                        f"ReviewRecorded: failure_kind must be non-empty str, "
                        f"got {fk!r}"
                    )
                failure_kinds[key] = fk
            else:
                failure_kinds.pop(key, None)
        elif t in ("ResultRecorded", "AttemptClosed"):
            aid = _require_nonempty_str(p, "attempt_id", event_type=t)
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
