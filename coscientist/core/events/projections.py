"""Fold events into projection tables (DESIGN §3.5a)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.events import StoredEvent

log = logging.getLogger(__name__)

# table -> row_key -> data
ProjectionMap = dict[str, dict[str, dict[str, Any]]]

# When set, ``_put`` records (table, key) for dirty-only SQLite upserts.
_DIRTY_KEYS: ContextVar[set[tuple[str, str]] | None] = ContextVar(
    "projection_dirty_keys", default=None
)


def _put(proj: ProjectionMap, table: str, key: str, data: dict[str, Any]) -> None:
    if table not in proj:
        proj[table] = {}
    proj[table][key] = data
    dirty = _DIRTY_KEYS.get()
    if dirty is not None:
        dirty.add((table, key))


def _get(proj: ProjectionMap, table: str, key: str) -> dict[str, Any] | None:
    bucket = proj.get(table)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if bucket is None:
        return None
    return bucket.get(key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)


def _row_or_empty(proj: ProjectionMap, table: str, key: str) -> dict[str, Any]:
    """Return existing projection row or structural empty for first fold."""
    prev = _get(proj, table, key)
    if prev is None:
        return {}
    return dict(prev)


def _delegated_or_absent(p: dict[str, Any]) -> bool:
    if "delegated" not in p or p["delegated"] is None:
        return False
    delegated = p["delegated"]
    if not isinstance(delegated, bool):
        raise TypeError(f"delegated must be bool, got {type(delegated).__name__}")
    return delegated

def apply_to_map(proj: ProjectionMap, event: StoredEvent) -> None:
    p = event.payload
    t = event.type
    if t == "StudyCreated":
        _put(proj, "studies", event.study_id, dict(p))
    elif t == "TaskCreated" or t == "TaskInherited":
        _put(proj, "tasks", event.entity_key, dict(p))
    elif t == "PlanAccepted":
        _put(proj, "plans", event.entity_key, dict(p))
    elif t == "PlanExpanded":
        _put(proj, "plans", event.entity_key, dict(p))
    # ExpansionRefused: journal-only audit (runtime folds to _expansion_refusals).
    elif t == "StudyCancelled":
        study = _row_or_empty(proj, "studies", event.study_id)
        _put(proj, "studies", event.study_id, {**study, "cancelled": True, **p})
    elif t == "StudyRefused":
        study = _row_or_empty(proj, "studies", event.study_id)
        _put(proj, "studies", event.study_id, {**study, "refused": True, **p})
    elif t == "BudgetExceeded":
        study = _row_or_empty(proj, "studies", event.study_id)
        _put(
            proj,
            "studies",
            event.study_id,
            {**study, "budget_exceeded": True, **p},
        )
    elif t == "StudyFinalized":
        study = _row_or_empty(proj, "studies", event.study_id)
        meta = _row_or_empty(proj, "meta", event.study_id)
        # First-wins: legacy journals may contain a second StudyFinalized; keep
        # the first payload/graph and mark the I3 breach as taint (not silent).
        if study.get("finalized") or meta.get("finalized"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal
            try:
                _seq = event.seq
            except AttributeError:
                _seq = None
            log.warning(
                "ignoring duplicate StudyFinalized study_id=%s seq=%s "
                "(first-wins; payload not applied; meta tainted)",
                event.study_id,
                _seq,
            )
            _put(
                proj,
                "meta",
                event.study_id,
                {
                    **meta,
                    "study_id": event.study_id,
                    "finalized": True,
                    "duplicate_study_finalized": True,
                    "tainted": True,
                },
            )
            return
        _put(
            proj,
            "studies",
            event.study_id,
            {**study, "finalized": True, "finalized_payload": p},
        )
        _put(
            proj,
            "meta",
            event.study_id,
            {"study_id": event.study_id, "finalized": True},
        )
        from coscientist.core.finalize.finalize_nodes import graph_rows_from_finalized

        nodes, edges = graph_rows_from_finalized(event.study_id, p, created_at=event.ts)
        for key, row in nodes.items():
            _put(proj, "graph_nodes", key, row)
        for key, row in edges.items():
            _put(proj, "graph_edges", key, row)
    elif t == "AttemptStarted":
        _put(proj, "attempts", event.entity_key, dict(p))
        task = _get(proj, "tasks", p["task_id"])
        if task is not None:
            attempts = (
                list(task["attempts"]) if "attempts" in task else []  # audit-ok[ternary_name]: structural empty = no attempts
            )
            if event.entity_key not in attempts:
                attempts.append(event.entity_key)
            task = {
                **task,
                "state": "RUNNING",
                "attempts": attempts,
            }
            _put(proj, "tasks", p["task_id"], task)
    elif t == "AttemptClosed":
        att = _row_or_empty(proj, "attempts", event.entity_key)
        att = {**att, **p, "closed": True}
        _put(proj, "attempts", event.entity_key, att)
        # P3: retryable close → task PENDING with retry_at (no RUNNING without attempt)
        tid = p.get("task_id") or att.get("task_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal
        if tid and p.get("retry_at"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            task = _get(proj, "tasks", tid)
            if task is not None and task.get("state") != "DONE":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                _put(
                    proj,
                    "tasks",
                    tid,
                    {
                        **task,
                        "state": "PENDING",
                        "reason": TaskReason.RETRY_SCHEDULED.value,
                        "retry_at": p["retry_at"],
                    },
                )
    elif t == "RouteReturned":
        att = _row_or_empty(proj, "attempts", event.entity_key)
        att = {**att, "route_returned": True, "agent_output_digest": p.get("agent_output_digest")}  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        _put(proj, "attempts", event.entity_key, att)
    elif t == "ResultRecorded":
        _put(proj, "results", event.entity_key, dict(p))
    elif t == "TaskDone":
        task = _row_or_empty(proj, "tasks", event.entity_key)
        task = {
            **task,
            "state": "DONE",
            "outcome": p.get("outcome"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            "reason": p.get("reason"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        }
        _put(proj, "tasks", event.entity_key, task)
    elif t == "ArtifactRegistered":
        _put(proj, "artifacts", event.entity_key, dict(p))
    elif t == "EffectIntended":
        row = {**p, "key": p["key"], "intended": True}
        _put(proj, "effects", event.entity_key, row)
    elif t == "EffectDone":
        prev = _row_or_empty(proj, "effects", event.entity_key)
        row = {
            **prev,
            **p,
            "key": p["key"],
            "intended": True,
            "done": True,
        }
        _put(proj, "effects", event.entity_key, row)
    elif t == "SideEffectDenied":
        _put(
            proj,
            "effects",
            event.entity_key,
            {**p, "key": p["key"], "denied": True},
        )
    elif t == "AttemptTainted":
        # Sticky attempt taint after source fetch (write/external deny).
        # Do not clobber an existing attempt ``reason`` with the taint payload.
        if "attempt_id" in p and p["attempt_id"] is not None:
            raw_aid = p["attempt_id"]
            if not isinstance(raw_aid, str):
                raise TypeError(f"attempt_id must be str, got {type(raw_aid).__name__}")
            aid = raw_aid
        elif event.entity_key:
            aid = event.entity_key
        else:
            raise TypeError("ToolEffect event missing attempt_id/entity_key")
        if aid:
            prev = _row_or_empty(proj, "attempts", aid)
            kept_reason = prev.get("reason")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            row = {**prev, **p, "tainted_context": True}
            if kept_reason is not None:
                row["reason"] = kept_reason
            _put(proj, "attempts", aid, row)
        # Pairing marker: trust journaled tool + is_fetch (see note_fetch_call…).
        # Reclassified live registry → domain error caught here: do not brick fold.
        from coscientist.core.events.errors import FetchToolReclassified
        from coscientist.core.execution.source_fetch import note_fetch_call_from_attempt_tainted

        if not isinstance(p, dict):
            raise TypeError(f"AttemptTainted payload must be dict, got {type(p).__name__}")
        try:
            marked = note_fetch_call_from_attempt_tainted(p)
        except FetchToolReclassified as exc:
            try:
                key = exc.call_id
            except AttributeError:
                key = None
            if not key:
                try:
                    key = exc.tool
                except AttributeError:
                    key = None
            if not isinstance(key, str) or not key:
                key = ""
            if key:
                _put(
                    proj,
                    "fetch_integrity",
                    key,
                    {
                        "kind": "fetch_tool_reclassified",
                        "tool": exc.tool,
                        # Persist the projection key even when call_id was empty
                        # so ``_row_key`` can reload the row after a store round-trip.
                        "call_id": exc.call_id or key,  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
                        "tool_name": exc.tool,
                        "attempt_id": aid,
                        "row_key": key,
                        "internal_error": True,
                    },
                )
            marked = None
        if marked is not None:
            call_id, tool = marked
            _put(
                proj,
                "fetch_calls",
                call_id,
                {"call_id": call_id, "tool": tool, "attempt_id": aid},
            )
    elif t == "FetchIntegrityRaised":
        if not isinstance(p, dict):
            raise TypeError(f"event payload must be dict, got {type(p).__name__}")
        payload = p
        # Prefer non-empty call_id; empty string falls through (tool-only rows).
        raw_cid = payload.get("call_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if isinstance(raw_cid, str) and raw_cid:
            raw_key = raw_cid
        elif event.entity_key is not None and event.entity_key != "":
            raw_key = event.entity_key
        elif isinstance(payload.get("tool"), str) and payload["tool"]:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            raw_key = payload["tool"]
        elif isinstance(payload.get("row_key"), str) and payload["row_key"]:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            raw_key = payload["row_key"]
        else:
            raw_key = ""
        if not isinstance(raw_key, str):
            raise TypeError(f"fetch integrity key must be str, got {type(raw_key).__name__}")
        key = raw_key
        if key:
            if isinstance(raw_cid, str) and raw_cid:
                call_id = raw_cid
            else:
                call_id = key
            _put(
                proj,
                "fetch_integrity",
                key,
                {
                    **payload,
                    "call_id": call_id,
                    "row_key": key,
                    "internal_error": True,
                },
            )
    elif t == "SourceFetched":
        # Only fold when paired with a journaled fetch ToolCall (AttemptTainted).
        from coscientist.core.execution.source_fetch import source_fetched_paired_with_fetch_call

        if not isinstance(p, dict):
            raise TypeError(f"event payload must be dict, got {type(p).__name__}")
        payload = p
        if "call_id" not in payload or payload["call_id"] is None:
            call_id = ""
        else:
            raw_cid = payload["call_id"]
            if not isinstance(raw_cid, str):
                raise TypeError(f"call_id must be str, got {type(raw_cid).__name__}")
            call_id = raw_cid
        paired = _get(proj, "fetch_calls", call_id) if call_id else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal

        if (
            paired is not None
            and "tool" in paired
            and isinstance(paired["tool"], str)
        ):
            fetch_calls = {call_id: paired["tool"]}
        else:
            fetch_calls = {}
        if not source_fetched_paired_with_fetch_call(payload, fetch_calls):
            return
        if "digest" not in payload or payload["digest"] is None:
            digest = ""
        else:
            raw_d = payload["digest"]
            if not isinstance(raw_d, str):
                raise TypeError(f"digest must be str, got {type(raw_d).__name__}")
            digest = raw_d
        row_key = digest or event.entity_key  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        _put(proj, "source_fetches", row_key, {**payload, "digest": digest})
    elif t == "HitlOpened":
        _put(proj, "hitl_requests", event.entity_key, {**p, "open": True})
    elif t in ("HitlDecided", "HitlTimedOut"):
        hitl = _row_or_empty(proj, "hitl_requests", event.entity_key)
        if "keep_open" in p and p["keep_open"] is not None:
            if not isinstance(p["keep_open"], bool):
                raise TypeError(f"keep_open must be bool, got {type(p['keep_open']).__name__}")
            keep_open = p["keep_open"]
        else:
            keep_open = p.get("effect") == "WAIT"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        hitl = {**hitl, **p, "open": keep_open}
        _put(proj, "hitl_requests", event.entity_key, hitl)
    elif t == "HitlDeadlineExtended":
        hitl = _row_or_empty(proj, "hitl_requests", event.entity_key)
        hitl = {**hitl, "deadline": p.get("deadline", hitl.get("deadline"))}  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[dict_get_default]: explicit default only for structural absent key  # audit-ok[dict_get_default]: explicit default only for structural absent key
        _put(proj, "hitl_requests", event.entity_key, hitl)
    elif t == "GraphNodeUpserted":
        _put(proj, "graph_nodes", event.entity_key, dict(p))
    elif t == "GraphEdgeAdded":
        _put(proj, "graph_edges", event.entity_key, dict(p))
    elif t == "QuestionDisposed":
        qid = str(p.get("question_id") or event.entity_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        _put(proj, "question_disposals", qid, dict(p))
    elif t == "QuestionDisposeRejected":
        # Key by idem-stable detail digest so restarts keep every distinct reject.
        if "detail" not in p or p["detail"] is None:
            detail = ""
        else:
            raw_detail = p["detail"]
            if not isinstance(raw_detail, str):
                raise TypeError(f"detail must be str, got {type(raw_detail).__name__}")
            detail = raw_detail
        if "question_id" in p and p["question_id"] is not None:
            raw_qid = p["question_id"]
            if not isinstance(raw_qid, str):
                raise TypeError(f"question_id must be str, got {type(raw_qid).__name__}")
            qid = raw_qid
        elif event.entity_key:
            qid = event.entity_key
        else:
            qid = ""
        from coscientist.core.events.digest import sha256_hex

        key = f"{qid}:{sha256_hex(detail)[:16]}"
        _put(proj, "disposal_rejects", key, {**dict(p), "row_key": key})
    elif t == "RequirementUpserted":
        _put(proj, "requirements", event.entity_key, dict(p))
    elif t == "ReviewScheduled":
        # tracked on requirements / results; keep a lightweight row
        _put(proj, "requirements", f"review:{event.entity_key}", dict(p))
    elif t == "CheckProposed":
        pid = str(p.get("proposal_id") or event.entity_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        _put(proj, "check_proposals", pid, {**p, "proposal_id": pid, "status": "proposed"})
    elif t == "CheckApproved":
        pid = str(p.get("proposal_id") or event.entity_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        prev = _get(proj, "check_proposals", pid) or {"proposal_id": pid}
        _put(
            proj,
            "check_proposals",
            pid,
            {
                **prev,
                **p,
                "status": "approved",
                "delegated": _delegated_or_absent(p),
            },
        )
    elif t == "CheckRejected":
        pid = str(p.get("proposal_id") or event.entity_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        prev = _get(proj, "check_proposals", pid) or {"proposal_id": pid}
        _put(
            proj,
            "check_proposals",
            pid,
            {
                **prev,
                **p,
                "status": "rejected",
                "reject_reason": p.get("reason"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            },
        )

def fold_events(events: Sequence[StoredEvent]) -> ProjectionMap:
    proj: ProjectionMap = {}  # audit-ok[field_default]: structural empty = new projection map
    for ev in events:
        apply_to_map(proj, ev)
    return proj

def _projection_load_tables(event_type: str) -> tuple[str, ...]:
    """Tables ``apply_to_map`` may read or write for this event (not the world)."""
    affected = _affected_tables(event_type)
    # Read deps that are not always in the write set.
    extra: tuple[str, ...] = ()  # audit-ok[field_default]: structural empty = no extra tables
    if event_type == "SourceFetched":
        extra = ("fetch_calls",)
    # Dedup preserve order.
    seen: set[str] = set()
    out: list[str] = []  # audit-ok[field_default]: structural empty = collecting table names
    for t in (*affected, *extra):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return tuple(out)

def apply_event(store: Any, event: StoredEvent) -> None:
    """Apply one event into the store's projection table (in-txn write connection).

    Uses ``_get_projection_write`` / ``_upsert_projection_write`` when present so
    projection updates stay on the fenced writer connection inside append.

    Only loads tables this event needs and only upserts rows ``apply_to_map``
    actually changed. The previous full-table rewrite (every attempt/task on
    every ``RouteReturned``) sat on the fsync-critical path and produced
    O(n²) SQLite work under the §3.5a parallel load — the rt_exp p99 tail.
    """
    try:
        get_rows = store._get_projection_write
        put_row = store._upsert_projection_write
    except AttributeError:
        get_rows = None
        put_row = None
    if get_rows is None or put_row is None:
        raise TypeError("apply_event requires a write-capable store (_get_projection_write / _upsert_projection_write)")
    load_tables = _projection_load_tables(event.type)
    if not load_tables:
        return
    scratch: ProjectionMap = {}  # audit-ok[field_default]: structural empty = scratch projection
    for table in load_tables:
        for row in get_rows(table):
            key = _row_key(table, row)
            if key:
                if table not in scratch:
                    scratch[table] = {}
                scratch[table][key] = row
    dirty: set[tuple[str, str]] = set()
    token = _DIRTY_KEYS.set(dirty)
    try:
        apply_to_map(scratch, event)
    finally:
        _DIRTY_KEYS.reset(token)
    affected = set(_affected_tables(event.type))
    for table, key in dirty:
        if table not in affected:
            continue
        _bucket = scratch.get(table)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        data = None if _bucket is None else _bucket.get(key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value) # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
        if data is not None:
            put_row(table, key, data)

def _row_key(table: str, row: dict[str, Any]) -> str | None:
    mapping = {
        "studies": "study_id",
        "plans": "plan_id",
        "tasks": "task_id",
        "attempts": "attempt_id",
        "results": "result_id",
        "artifacts": "artifact_id",
        "effects": "key",
        "source_fetches": "digest",
        "fetch_calls": "call_id",
        "requirements": "id",
        "hitl_requests": "request_id",
        "check_proposals": "proposal_id",
        "graph_nodes": "node_id",
        "graph_edges": "edge_id",
        "question_disposals": "question_id",
        "disposal_rejects": "row_key",
        "meta": "study_id",
    }
    if table == "fetch_integrity":
        # Key order (first non-empty wins): call_id → tool → tool_name → row_key.
        # call_id may be absent on tool-only integrity rows — never drop them.
        for cand in (
            row.get("call_id"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            row.get("tool"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            row.get("tool_name"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            row.get("row_key"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        ):
            if cand:
                return str(cand)
        return None
    field = mapping.get(table)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if table == "meta":
        if "study_id" in row and row["study_id"] is not None:
            sid = row["study_id"]
            if not isinstance(sid, str):
                raise TypeError(f"study_id must be str, got {type(sid).__name__}")
            return sid
        return row.get("row_key")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if field and field in row and row[field] is not None:
        val = row[field]
        if not isinstance(val, str):
            raise TypeError(f"{field} must be str, got {type(val).__name__}")
        return val
    return row.get("row_key")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)

def _affected_tables(event_type: str) -> tuple[str, ...]:
    tables: dict[str, tuple[str, ...]] = {
        "StudyCreated": ("studies",),
        "TaskCreated": ("tasks",),
        "TaskInherited": ("tasks",),
        "PlanAccepted": ("plans",),
        "PlanExpanded": ("plans",),
        "StudyCancelled": ("studies",),
        "StudyRefused": ("studies",),
        "BudgetExceeded": ("studies",),
        "StudyFinalized": ("studies", "meta", "graph_nodes", "graph_edges"),
        "AttemptStarted": ("attempts", "tasks"),
        "AttemptClosed": ("attempts", "tasks"),
        "RouteReturned": ("attempts",),
        "ResultRecorded": ("results",),
        "TaskDone": ("tasks",),
        "ArtifactRegistered": ("artifacts",),
        "EffectIntended": ("effects",),
        "EffectDone": ("effects",),
        "SideEffectDenied": ("effects",),
        "SourceFetched": ("source_fetches",),
        "AttemptTainted": ("attempts", "fetch_calls", "fetch_integrity"),
        "FetchIntegrityRaised": ("fetch_integrity",),
        "HitlOpened": ("hitl_requests",),
        "HitlDecided": ("hitl_requests",),
        "HitlTimedOut": ("hitl_requests",),
        "HitlDeadlineExtended": ("hitl_requests",),
        "CheckProposed": ("check_proposals",),
        "CheckApproved": ("check_proposals",),
        "CheckRejected": ("check_proposals",),
        "GraphNodeUpserted": ("graph_nodes",),
        # kind=decomposes (PR16) writes graph_edges; keep in the set so #27's
        # dirty-key apply_event path still projects those edges.
        "GraphEdgeAdded": ("graph_edges",),
        "QuestionDisposed": ("question_disposals",),
        "QuestionDisposeRejected": ("disposal_rejects",),
        "RequirementUpserted": ("requirements",),
        "ReviewScheduled": ("requirements",),
    }
    if event_type not in tables:
        return ()
    return tables[event_type]
