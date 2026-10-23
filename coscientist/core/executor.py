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
from coscientist.core.ids import result_id
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


def _derive_claim_core_attrs(task: Any, task_outcome: TaskOutcome) -> dict[str, Any]:
    """Core-owned counting fields from plan/results — never agent claim attrs."""
    spec = getattr(task, "spec", None)
    method = getattr(spec, "method", None) if spec is not None else None
    lit = method == ExperimentMethod.LITERATURE_SEARCH or str(method or "") == (
        "literature_search"
    )
    success = task_outcome == TaskOutcome.SUCCESS
    criteria = list(getattr(spec, "criteria", None) or []) if spec is not None else []
    return {
        "criterion_status": "met" if success else "unassessed",
        "evidence_kind": "literature" if lit else "computational",
        # Plan criteria are treated frozen at accept until freeze events exist.
        "frozen": True,
        "lit_own_criterion": bool(lit and criteria),
        "accepted": bool(success),
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
        payload["payload_digest"] = payload_digest(payload)
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome(final.value),
            None,
            claims=list(out.claims or []),
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
        # Carry-over #2: do not emit claim nodes from a FAILED attempt —
        # they must not become bindable inputs.
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome.FAILED,
            reason,
            claims=[],
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
    ) -> Result:
        # Attempt-open check runs inside the write transaction (_append_many_sync).
        now = self.clock.now()
        graph = self.get_graph() if self.get_graph is not None else None
        task = self.get_task(task_id)
        derived = _derive_claim_core_attrs(task, task_outcome)
        # PR7/PR8: claim nodes carry producing-attempt provenance (H-b);
        # re-upsert keeps worst provenance; about edges + attr whitelist.
        claim_events, drops = claim_nodes_from_attempt(
            study_id=study_id,
            attempt_id=attempt_id,
            provenance=payload["provenance"],
            claims=list(claims or []),
            now=now,
            graph=graph,
            attempt_outcome=task_outcome.value,
            derived=derived,
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
