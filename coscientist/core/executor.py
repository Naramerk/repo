"""Executor.record — sole result-write path (DESIGN §4)."""

from __future__ import annotations

import logging
from typing import Any

from coscientist.checks.evaluate import evaluate_task_checks
from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.digest import payload_digest
from coscientist.core.domain import (
    AgentTaskOutput,
    Catalog,
    Outcome,
    ReqStatus,
    Result,
    TaskOutcome,
)
from coscientist.core.errors import AttemptClosedError, CatalogError
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import result_id
from coscientist.core.outcome import compute_outcome
from coscientist.core.writer import StudyWriter

log = logging.getLogger(__name__)


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
        review_statuses: dict[str, ReqStatus] | None = None,
    ) -> None:
        self.writer = writer
        self.catalog = catalog
        self.artifacts = artifacts
        self.clock = clock
        self.get_attempt = get_attempt
        self.get_task = get_task
        self.review_statuses = review_statuses or {}
        self._scheduled_reviews: list[str] = []

    def record(self, attempt_id: str, out: AgentTaskOutput) -> Result:
        """Synchronous API used by tests; schedules async write via writer when running."""
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        if attempt.closed:
            # late write rejected
            self._emit_sync(
                EventEnvelope(
                    study_id=attempt.study_id,
                    type="LateWriteRejected",
                    entity_key=attempt_id,
                    idem_key=make_idem("LateWriteRejected", attempt_id, str(self.clock.now())),
                    payload={"attempt_id": attempt_id, "reason": "attempt_closed"},
                    ts=self.clock.now(),
                )
            )
            raise AttemptClosedError(attempt_id)

        task = self.get_task(attempt.task_id)
        checks = evaluate_task_checks(
            self.catalog,
            task.spec,
            out,
            self.artifacts,
            review_statuses=self.review_statuses,
        )
        # schedule reviews
        for c in checks:
            if not c.is_code and c.status == ReqStatus.UNASSESSED:
                self._scheduled_reviews.append(c.check_id)
                self._emit_sync(
                    EventEnvelope(
                        study_id=attempt.study_id,
                        type="ReviewScheduled",
                        entity_key=attempt_id,
                        idem_key=make_idem("ReviewScheduled", attempt_id, c.check_id),
                        payload={"check_id": c.check_id, "attempt_id": attempt_id},
                        ts=self.clock.now(),
                    )
                )

        claim = out.claim
        reason = None
        if out.downgrade is not None:
            claim = out.downgrade.to
            reason = out.downgrade.reason or None
            if not out.downgrade.refs:
                reason = None  # free text without refs ignored (M1 strictness soft in M0)

        provenance = getattr(attempt, "provenance", None) or "real"
        try:
            computed, downgrade = compute_outcome(
                checks, claim, reason, provenance
            )
        except CatalogError as e:
            rid = result_id(attempt_id)
            payload = {
                "result_id": rid,
                "attempt_id": attempt_id,
                "outcome": Outcome.PARTIAL.value,
                "computed_outcome": Outcome.PARTIAL.value,
                "reason": "catalog_error",
                "provenance": provenance,
                "check_results": [],
                "unassessed": [],
                "artifacts": [h.artifact_id for h in out.outputs.values()],
                "payload_digest": "",
                "agent_claim": claim.value if claim else None,
                "inputs": [],
            }
            payload["payload_digest"] = payload_digest(payload)
            self._emit_sync(
                EventEnvelope(
                    study_id=attempt.study_id,
                    type="CatalogErrorRaised",
                    entity_key=attempt_id,
                    idem_key=make_idem("CatalogErrorRaised", attempt_id),
                    payload={"error": str(e)},
                    ts=self.clock.now(),
                )
            )
            return self._commit_result(attempt, task, payload, TaskOutcome.PARTIAL, "catalog_error")

        if computed is None:
            # still waiting — leave RUNNING (M0 tests usually force review statuses)
            raise RuntimeError("compute_outcome pending; supply review statuses or wait")

        if claim and reason is None and out.downgrade is not None:
            log.info("downgrade without reason ignored claim=%s", claim)

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
            "downgrade": downgrade.model_dump(mode="json", by_alias=True) if downgrade else None,
            "inputs": [],
        }
        payload["payload_digest"] = payload_digest(payload)
        task_outcome = TaskOutcome(final.value)
        return self._commit_result(attempt, task, payload, task_outcome, None)

    def _commit_result(
        self,
        attempt: Any,
        task: Any,
        payload: dict[str, Any],
        task_outcome: TaskOutcome,
        reason: str | None,
    ) -> Result:
        # re-check closed inside the "transaction" (sync path)
        fresh = self.get_attempt(attempt.attempt_id)
        if fresh is not None and fresh.closed:
            raise AttemptClosedError(attempt.attempt_id)

        events = [
            EventEnvelope(
                study_id=attempt.study_id,
                type="ResultRecorded",
                entity_key=payload["result_id"],
                idem_key=make_idem("ResultRecorded", payload["result_id"]),
                payload=payload,
                ts=self.clock.now(),
            ),
            EventEnvelope(
                study_id=attempt.study_id,
                type="AttemptClosed",
                entity_key=attempt.attempt_id,
                idem_key=make_idem("AttemptClosed", attempt.attempt_id, "result"),
                payload={
                    "attempt_id": attempt.attempt_id,
                    "closed": True,
                    "close_reason": "result",
                    "task_id": attempt.task_id,
                },
                ts=self.clock.now(),
            ),
            EventEnvelope(
                study_id=attempt.study_id,
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload={
                    "task_id": task.task_id,
                    "outcome": task_outcome.value,
                    "reason": reason,
                },
                ts=self.clock.now(),
            ),
        ]
        for ev in events:
            self._emit_sync(ev)

        return Result(
            result_id=payload["result_id"],
            attempt_id=attempt.attempt_id,
            outcome=Outcome(payload["outcome"]),
            provenance=payload.get("provenance", "real"),
            agent_claim=Outcome(payload["agent_claim"]) if payload.get("agent_claim") else None,
            computed_outcome=Outcome(payload["computed_outcome"]),
            artifacts=list(payload.get("artifacts", [])),
            unassessed=list(payload.get("unassessed", [])),
            payload_digest=payload["payload_digest"],
            reason=reason,
        )

    def _emit_sync(self, event: EventEnvelope) -> None:
        """Append synchronously (record is sync; DESIGN §4.1)."""
        from coscientist.core.errors import Idempotent

        try:
            self.writer.store._append_many_sync([event])  # noqa: SLF001
        except Idempotent:
            return
