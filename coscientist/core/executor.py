"""Executor.record — sole result-write path (DESIGN §4)."""

from __future__ import annotations

import logging
from typing import Any

from coscientist.checks.evaluate import evaluate_task_checks
from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.digest import canonical_json, payload_digest
from coscientist.core.domain import (
    VALID_PROVENANCE,
    AgentTaskOutput,
    Catalog,
    ExperimentMethod,
    Outcome,
    Provenance,
    ReqStatus,
    Result,
    TaskOutcome,
)
from coscientist.core.errors import AttemptClosedError, CatalogError
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import criterion_node_id, result_id
from coscientist.core.inputs import claim_nodes_from_attempt
from coscientist.core.outcome import compute_outcome
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.writer import StudyWriter

log = logging.getLogger(__name__)


def _provenance(attempt: Any) -> Provenance:
    p = getattr(attempt, "provenance", None)
    if p is None and isinstance(attempt, dict):
        p = attempt.get("provenance")
    if p not in VALID_PROVENANCE:
        raise ValueError(f"invalid attempt provenance: {p!r}")
    return p  # type: ignore[return-value]


def _req_to_criterion_status(status: ReqStatus) -> str:
    if status in (ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE):
        return "met"
    if status in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR):
        return "not_met"
    return "unassessed"


def _check_field(c: Any, name: str) -> Any:
    if isinstance(c, dict):
        return c.get(name)
    return getattr(c, name, None)


def _normalize_to_template_id(
    key: str, checks: list[Any], criteria: list[Any]
) -> str:
    """Map check_id (``value_match#0``) → template_id; leave template_id as-is."""
    for c in checks:
        tid = _check_field(c, "template_id")
        cid = _check_field(c, "check_id")
        if cid is not None and str(cid) == key and tid:
            return str(tid)
        if tid is not None and str(tid) == key:
            return str(tid)
    for ref in criteria:
        tid = _check_field(ref, "template_id")
        if tid is not None and str(tid) == key:
            return str(tid)
    return key


def _criterion_key_for_claim(
    raw: dict[str, Any], checks: list[Any], criteria: list[Any]
) -> str | None:
    """Resolve checked_against → template_id, else sole check / sole plan criterion."""
    checked = raw.get("checked_against")
    if checked:
        return _normalize_to_template_id(str(checked), checks, criteria)
    if len(checks) == 1:
        tid = _check_field(checks[0], "template_id")
        return str(tid) if tid else None
    if len(criteria) == 1:
        tid = _check_field(criteria[0], "template_id")
        return str(tid) if tid else None
    return None


def _criterion_status_for_claim(
    raw: dict[str, Any], checks: list[Any], criteria: list[Any] | None = None
) -> str:
    """Map claim.checked_against → ReqStatus of that criterion's check."""
    crit = list(criteria or [])
    key = _criterion_key_for_claim(raw, checks, crit)
    if key:
        for c in checks:
            tid = _check_field(c, "template_id")
            cid = _check_field(c, "check_id")
            st = _check_field(c, "status")
            # Exact match only (no endswith — avoids C11 matching ``1``).
            if (tid is not None and str(tid) == key) or (
                cid is not None and str(cid) == key
            ):
                if isinstance(st, ReqStatus):
                    return _req_to_criterion_status(st)
                try:
                    return _req_to_criterion_status(ReqStatus(str(st)))
                except ValueError:
                    return "unassessed"
        return "unassessed"
    if len(checks) == 1:
        st = _check_field(checks[0], "status")
        if isinstance(st, ReqStatus):
            return _req_to_criterion_status(st)
    return "unassessed"


def _check_role(c: Any) -> str:
    role = _check_field(c, "role")
    if role in ("prediction", "validity"):
        return str(role)
    return "validity"


def _required_validity_failed(checks: list[Any]) -> bool:
    """True when a required validity check FAILED (Duhem–Quine cap)."""
    for c in checks:
        if _check_role(c) != "validity":
            continue
        if not bool(_check_field(c, "required")):
            continue
        st = _check_field(c, "status")
        if isinstance(st, ReqStatus):
            failed = st in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR)
        else:
            failed = str(st) in (
                ReqStatus.FAILED.value,
                ReqStatus.MIGRATION_ERROR.value,
            )
        if failed:
            return True
    return False


def _role_for_claim_key(
    raw: dict[str, Any], checks: list[Any], criteria: list[Any]
) -> str:
    """Role of the check this claim is checked_against (default validity)."""
    key = _criterion_key_for_claim(raw, checks, criteria)
    if key:
        for c in checks:
            tid = _check_field(c, "template_id")
            cid = _check_field(c, "check_id")
            if (tid is not None and str(tid) == key) or (
                cid is not None and str(cid) == key
            ):
                return _check_role(c)
        for ref in criteria:
            tid = _check_field(ref, "template_id")
            if tid is not None and str(tid) == key:
                role = _check_field(ref, "role")
                if role in ("prediction", "validity"):
                    return str(role)
                # Kind heuristic when CheckResult role missing.
                from coscientist.checks.evaluate import resolve_check_role
                from coscientist.core.domain import RequirementRef

                if isinstance(ref, RequirementRef):
                    return resolve_check_role(ref)
                return resolve_check_role(
                    RequirementRef(template_id=str(tid), role=role)
                )
    if len(checks) == 1:
        return _check_role(checks[0])
    return "validity"


def _criterion_is_frozen(graph: Any, study_id: str, template_id: Any) -> bool:
    """True when plan-accept freeze marked the criterion node (keyed by template_id)."""
    if not template_id or graph is None:
        return False
    key = str(template_id)
    node = graph.get_node(criterion_node_id(study_id, key))
    if node is None:
        node = graph.get_node(key)
    if node is None or node.kind != "criterion":
        return False
    attrs = node.attrs or {}
    return attrs.get("frozen_at_seq") is not None or bool(attrs.get("frozen"))


def _derive_claim_core_attrs(
    task: Any,
    task_outcome: TaskOutcome,
    *,
    raw: dict[str, Any],
    checks: list[Any],
    graph: Any,
    study_id: str,
    validated: bool,
) -> dict[str, Any]:
    """Core-owned counting fields from plan/checks/freeze — never agent attrs.

    ``accepted`` means the result was recorded and the claim is structurally
    valid — **not** that criteria passed (graph_schema invariant 10).

    Check ``role`` (Duhem–Quine):
    - ``prediction`` FAILED → ``criterion_status=not_met`` → counting refute.
    - required ``validity`` FAILED → claim not counted (``validity_ok=False``);
      criterion_status stays ``unassessed`` (never refuted from that experiment).
    """
    del task_outcome  # outcome is scientific whenever we derive; criteria ≠ accepted
    spec = getattr(task, "spec", None)
    method = getattr(spec, "method", None) if spec is not None else None
    lit = method == ExperimentMethod.LITERATURE_SEARCH or str(method or "") == (
        "literature_search"
    )
    criteria = list(getattr(spec, "criteria", None) or []) if spec is not None else []
    key = _criterion_key_for_claim(raw, checks, criteria)
    role = _role_for_claim_key(raw, checks, criteria)
    status = _criterion_status_for_claim(raw, checks, criteria)
    validity_ok = True
    # Any required validity failure caps this experiment: no counting refutation.
    if _required_validity_failed(checks):
        validity_ok = False
        status = "unassessed"
    elif role == "validity" and status == "not_met":
        validity_ok = False
        status = "unassessed"
    return {
        "criterion_status": status,
        "evidence_kind": "literature" if lit else "computational",
        "frozen": _criterion_is_frozen(graph, study_id, key),
        "lit_own_criterion": bool(lit and criteria),
        "accepted": bool(validated),
        "validity_ok": validity_ok,
        "check_role": role,
    }


class Executor:
    def __init__(
        self,
        writer: StudyWriter,
        catalog: Catalog,
        artifacts: LocalArtifactStore,
        *,
        clock: Any,
        get_attempt: Any,
        get_task: Any,
        on_events: Any | None = None,
        review_statuses: dict[str, ReqStatus] | None = None,
        get_graph: Any | None = None,
    ) -> None:
        self.writer = writer
        self.catalog = catalog
        self.artifacts = artifacts
        self.clock = clock
        self.get_attempt = get_attempt
        self.get_task = get_task
        self.on_events = on_events  # callback after successful append (project local)
        self.review_statuses = review_statuses or {}
        self.get_graph = get_graph
        self._scheduled_reviews: list[str] = []

    async def mark_route_returned(self, attempt_id: str, out: AgentTaskOutput) -> str:
        """Persist agent output in CAS and emit RouteReturned with its digest."""
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = attempt.closed if hasattr(attempt, "closed") else attempt.get("closed")
        study_id = attempt.study_id if hasattr(attempt, "study_id") else attempt["study_id"]
        if closed:
            raise AttemptClosedError(attempt_id)
        content = canonical_json(out.model_dump(mode="json"))
        digest = self.artifacts.put_cas(content, tmp_name=f"route:{attempt_id}")
        await self._append(
            [
                EventEnvelope(
                    study_id=study_id,
                    type="RouteReturned",
                    entity_key=attempt_id,
                    idem_key=make_idem("RouteReturned", attempt_id),
                    payload={
                        "attempt_id": attempt_id,
                        "agent_output_digest": digest,
                    },
                    ts=self.clock.now(),
                )
            ],
            require_open_attempt=attempt_id,
        )
        return digest

    async def record(self, attempt_id: str, out: AgentTaskOutput) -> Result:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = attempt.closed if hasattr(attempt, "closed") else attempt.get("closed")
        study_id = attempt.study_id if hasattr(attempt, "study_id") else attempt["study_id"]
        task_id = attempt.task_id if hasattr(attempt, "task_id") else attempt["task_id"]

        if closed:
            await self._append(
                [
                    EventEnvelope(
                        study_id=study_id,
                        type="LateWriteRejected",
                        entity_key=attempt_id,
                        idem_key=make_idem(
                            "LateWriteRejected", attempt_id, str(self.clock.now())
                        ),
                        payload={"attempt_id": attempt_id, "reason": "attempt_closed"},
                        ts=self.clock.now(),
                    )
                ]
            )
            raise AttemptClosedError(attempt_id)

        task = self.get_task(task_id)
        checks = evaluate_task_checks(
            self.catalog,
            task.spec,
            out,
            self.artifacts,
            review_statuses=self.review_statuses,
        )
        for c in checks:
            if not c.is_code and c.status == ReqStatus.UNASSESSED:
                self._scheduled_reviews.append(c.check_id)

        claim = out.claim
        reason: str | None = None
        if out.downgrade is not None:
            if not out.downgrade.refs or not out.downgrade.reason:
                await self._append(
                    [
                        EventEnvelope(
                            study_id=study_id,
                            type="DowngradeRejected",
                            entity_key=attempt_id,
                            idem_key=make_idem("DowngradeRejected", attempt_id),
                            payload={
                                "attempt_id": attempt_id,
                                "reason": "downgrade requires reason and refs",
                            },
                            ts=self.clock.now(),
                        )
                    ]
                )
            else:
                claim = out.downgrade.to
                reason = out.downgrade.reason

        provenance = _provenance(attempt)
        try:
            computed, downgrade = compute_outcome(checks, claim, reason, provenance)
        except CatalogError as e:
            return await self._commit_failure(
                attempt_id,
                study_id,
                task_id,
                out,
                provenance,
                claim,
                reason=TaskReason.CATALOG_ERROR.value,
                detail=str(e),
            )

        if computed is None:
            await self._append(
                [
                    EventEnvelope(
                        study_id=study_id,
                        type="ResultPending",
                        entity_key=attempt_id,
                        idem_key=make_idem(
                            "ResultPending", attempt_id, str(self.clock.now())
                        ),
                        payload={
                            "attempt_id": attempt_id,
                            "reason": "required_checks_pending",
                        },
                        ts=self.clock.now(),
                    )
                ]
            )
            raise RuntimeError("compute_outcome pending")

        for c in checks:
            if not c.is_code and c.status == ReqStatus.UNASSESSED:
                await self._append(
                    [
                        EventEnvelope(
                            study_id=study_id,
                            type="ReviewScheduled",
                            entity_key=attempt_id,
                            idem_key=make_idem(
                                "ReviewScheduled", attempt_id, c.check_id
                            ),
                            payload={"check_id": c.check_id, "attempt_id": attempt_id},
                            ts=self.clock.now(),
                        )
                    ]
                )

        unassessed = [c.check_id for c in checks if c.status == ReqStatus.UNASSESSED]
        rid = result_id(attempt_id)
        final = downgrade.to if downgrade else computed
        payload = {
            "result_id": rid,
            "attempt_id": attempt_id,
            "outcome": final.value,
            "computed_outcome": computed.value,
            "reason": None,
            "provenance": provenance,
            "check_results": [c.model_dump(mode="json") for c in checks],
            "unassessed": unassessed,
            "artifacts": [h.artifact_id for h in out.outputs.values()],
            "agent_claim": claim.value if claim else None,
            "downgrade": (
                downgrade.model_dump(mode="json", by_alias=True) if downgrade else None
            ),
            "inputs": [],
        }
        # Scientific result: criteria evaluated (SUCCESS / PARTIAL / FAILED).
        # Keep claims so not_met can refute (invariant 10); only infra drops them.
        payload["result_class"] = "scientific"
        payload["payload_digest"] = payload_digest(payload)
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome(final.value),
            None,
            claims=list(out.claims or []),
            checks=checks,
            infra_failure=False,
        )

    async def _commit_failure(
        self,
        attempt_id: str,
        study_id: str,
        task_id: str,
        out: AgentTaskOutput,
        provenance: Provenance,
        claim: Outcome | None,
        *,
        reason: str,
        detail: str,
    ) -> Result:
        """Infra/execution failure (catalog, crash path) — drop claims."""
        rid = result_id(attempt_id)
        payload = {
            "result_id": rid,
            "attempt_id": attempt_id,
            "outcome": Outcome.FAILED.value,
            "computed_outcome": Outcome.FAILED.value,
            "reason": reason,
            "provenance": provenance,
            "check_results": [],
            "unassessed": [],
            "artifacts": [h.artifact_id for h in out.outputs.values()],
            "agent_claim": claim.value if claim else None,
            "inputs": [],
            "detail": detail,
            "result_class": "infra",
        }
        payload["payload_digest"] = payload_digest(payload)
        await self._append(
            [
                EventEnvelope(
                    study_id=study_id,
                    type="CatalogErrorRaised",
                    entity_key=attempt_id,
                    idem_key=make_idem("CatalogErrorRaised", attempt_id),
                    payload={"error": detail, "reason": reason},
                    ts=self.clock.now(),
                )
            ]
        )
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome.FAILED,
            reason,
            claims=[],
            checks=[],
            infra_failure=True,
        )

    async def _commit_result(
        self,
        attempt_id: str,
        study_id: str,
        task_id: str,
        payload: dict[str, Any],
        task_outcome: TaskOutcome,
        reason: str | None,
        *,
        claims: list[dict[str, Any]] | None = None,
        checks: list[Any] | None = None,
        infra_failure: bool = False,
    ) -> Result:
        # Attempt-open check runs inside the write transaction (_append_many_sync).
        now = self.clock.now()
        graph = self.get_graph() if self.get_graph is not None else None
        task = self.get_task(task_id)
        # PR7/PR8: claim nodes carry producing-attempt provenance (H-b);
        # re-upsert keeps worst provenance; about edges + attr whitelist.
        # Scientific FAILED/PARTIAL keep claims (not_met → refute); infra drops.
        claim_events, drops = claim_nodes_from_attempt(
            study_id=study_id,
            attempt_id=attempt_id,
            provenance=payload["provenance"],
            claims=list(claims or []),
            now=now,
            graph=graph,
            attempt_outcome=task_outcome.value,
            infra_failure=infra_failure,
            derive_attrs=lambda raw, validated: _derive_claim_core_attrs(
                task,
                task_outcome,
                raw=raw,
                checks=list(checks or []),
                graph=graph,
                study_id=study_id,
                validated=validated,
            ),
        )
        if drops:
            payload = {**payload, "claims_dropped": drops}
            payload["payload_digest"] = payload_digest(payload)
        events = [
            *claim_events,
            EventEnvelope(
                study_id=study_id,
                type="ResultRecorded",
                entity_key=payload["result_id"],
                idem_key=make_idem("ResultRecorded", payload["result_id"]),
                payload=payload,
                ts=now,
                schema_version=2,
            ),
            EventEnvelope(
                study_id=study_id,
                type="AttemptClosed",
                entity_key=attempt_id,
                idem_key=make_idem("AttemptClosed", attempt_id, CloseReason.RESULT.value),
                payload={
                    "attempt_id": attempt_id,
                    "closed": True,
                    "close_reason": CloseReason.RESULT.value,
                    "task_id": task_id,
                },
                ts=now,
                schema_version=2,
            ),
            EventEnvelope(
                study_id=study_id,
                type="TaskDone",
                entity_key=task_id,
                idem_key=make_idem("TaskDone", task_id),
                payload={
                    "task_id": task_id,
                    "outcome": task_outcome.value,
                    "reason": reason,
                },
                ts=now,
                schema_version=2,
            ),
        ]
        await self._append(events, require_open_attempt=attempt_id)
        return Result(
            result_id=payload["result_id"],
            attempt_id=attempt_id,
            outcome=Outcome(payload["outcome"]),
            provenance=payload["provenance"],
            agent_claim=(
                Outcome(payload["agent_claim"]) if payload.get("agent_claim") else None
            ),
            computed_outcome=Outcome(payload["computed_outcome"]),
            artifacts=list(payload.get("artifacts", [])),
            unassessed=list(payload.get("unassessed", [])),
            payload_digest=payload["payload_digest"],
            reason=reason,
        )

    async def _append(
        self,
        events: list[EventEnvelope],
        *,
        require_open_attempt: str | None = None,
    ) -> None:
        stored = await self.writer.append_many(
            events, require_open_attempt=require_open_attempt
        )
        if self.on_events is not None:
            for ev in events:
                self.on_events(ev)
        _ = stored
