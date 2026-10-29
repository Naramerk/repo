"""HITL commands are consumed by the lease-owning run, never another writer."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from coscientist.agents.schemas import PlanOut
from coscientist.api.errors import ApiRefuse
from coscientist.api.runtime import StudyRuntime as ApiRuntime
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import HitlPrompt
from coscientist.experimental.runtime import StudyRuntime


class HitlRefuse(ApiRefuse):
    """Command cannot be applied to this exact live request."""


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Literal["accept", "revise", "reject"]
    opened_seq: StrictInt = Field(gt=0)
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Absent feedback is the historical accept/reject contract (HITL v2 adds the text).
    feedback: str | None

    @model_validator(mode="before")
    @classmethod
    def _historical_absent_feedback(cls, data: object) -> object:
        if isinstance(data, dict) and "feedback" not in data:
            return {**data, "feedback": None}
        return data

    @field_validator("feedback", mode="before")
    @classmethod
    def _strip_feedback(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not str:
            return value  # strict mode refuses non-str; never invented here
        stripped = value.strip()
        if not stripped:
            return None
        return stripped

    @model_validator(mode="after")
    def _feedback_contract(self) -> AnswerBody:
        if self.feedback is not None and len(self.feedback) > 4000:
            raise ValueError("hitl_feedback_too_long")
        if self.decision == "revise" and self.feedback is None:
            raise ValueError("hitl_feedback_required_for_revise")
        return self


@dataclass
class _Answer:
    body: AnswerBody
    done: asyncio.Future[dict[str, object]]


@dataclass(frozen=True)
class StageGateOutcome:
    """Typed result of one interactive stage-gate round (HITL v2, hitl-map §5).

    ``delegated`` means the human was silent past the deadline and the policy
    delegated the decision to the critic (on_timeout=delegate) — the run may
    proceed. ``revise``/``reject`` carry the human's exact feedback text.
    """

    decision: Literal["accept", "revise", "reject", "delegated"]
    feedback: str | None


class HitlOwner:
    """One bounded inbox for one API run; the run coroutine owns all effects."""

    runtime_cls = StudyRuntime

    def __init__(self, *, poll_s: float, lease_ttl_s: float) -> None:
        self._poll_s = poll_s
        self._ttl_s = lease_ttl_s
        self._queue: asyncio.Queue[_Answer] = asyncio.Queue(maxsize=1)
        self.waiting: tuple[str, str] | None
        self.waiting = None
        self.active_study_id: str | None
        self.active_study_id = None
        self._closed = False
        self._inflight = False

    def available(self, sid: str, rid: object) -> bool:
        return not self._closed and self.waiting == (sid, rid)

    async def submit(self, sid: str, rid: str, body: AnswerBody) -> dict[str, object]:
        if not self.available(sid, rid):
            raise HitlRefuse("hitl_owner_not_waiting")
        if self._inflight:
            raise HitlRefuse("hitl_answer_in_progress")
        self._inflight = True
        done: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Answer(body, done))
        try:
            # Disconnecting a browser must not cancel an already queued human command.
            return await asyncio.shield(done)
        finally:
            if done.done():
                self._inflight = False

    def close(self) -> None:
        self._closed = True
        self.waiting = None
        while not self._queue.empty():
            command = self._queue.get_nowait()
            if not command.done.done():
                command.done.set_exception(HitlRefuse("hitl_owner_closed"))

    async def review_plan(self, runtime: ApiRuntime, plan: PlanOut) -> bool:
        if not isinstance(runtime, StudyRuntime) or runtime.study is None:
            raise HitlRefuse("hitl_runtime_required")
        task = runtime._resolve_task("PLAN")
        attempts = [a for a in runtime.attempts.values() if a.get("task_id") == task.task_id]
        if not attempts:
            raise HitlRefuse("hitl_plan_attempt_required")
        attempt_id = str(attempts[-1]["attempt_id"])
        snapshot = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        prompt = HitlPrompt(
            question="Принять этот план исследования?",
            options=["accept", "reject"],
            context_json=snapshot,
            context_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
            attempt_id=attempt_id,
        )
        rid = await runtime.open_hitl("PLAN", kind="plan_approval", prompt=prompt)
        events = await runtime.store.read_all()
        opening = next(e for e in reversed(events) if e.type == "HitlOpened" and e.payload["request_id"] == rid)
        if opening.seq is None:
            raise HitlRefuse("hitl_opening_not_committed")
        sid = runtime.study.study_id
        self.active_study_id = sid
        self.waiting = (sid, rid)
        try:
            return await self._wait_answer(runtime, rid, opening.seq, prompt.context_sha256)
        finally:
            self.waiting = None

    async def review_stage(
        self,
        runtime: ApiRuntime,
        *,
        kind: str,
        task_key: str,
        attempt_id: str,
        question: str,
        snapshot: dict[str, object],
    ) -> StageGateOutcome:
        """One interactive stage gate (spec_approval/hypotheses/literature/report).

        Same owner inbox as ``review_plan``; timeout follows the §2 semantics for
        these kinds — on_timeout=delegate → the critic decides (unlike the plan
        gate, interactive silence here is NOT a study cancel). The caller owns
        the repair/rounds policy on top of the returned outcome.
        """
        if not isinstance(runtime, StudyRuntime) or runtime.study is None:
            raise HitlRefuse("hitl_runtime_required")
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        prompt = HitlPrompt(
            question=question,
            options=["accept", "reject"],
            context_json=snapshot_json,
            context_sha256=hashlib.sha256(snapshot_json.encode()).hexdigest(),
            attempt_id=attempt_id,
        )
        rid = await runtime.open_hitl(task_key, kind=kind, prompt=prompt)
        events = await runtime.store.read_all()
        opening = next(e for e in reversed(events) if e.type == "HitlOpened" and e.payload["request_id"] == rid)
        if opening.seq is None:
            raise HitlRefuse("hitl_opening_not_committed")
        sid = runtime.study.study_id
        self.active_study_id = sid
        self.waiting = (sid, rid)
        try:
            return await self._wait_stage_answer(runtime, rid, kind, opening.seq, prompt.context_sha256)
        finally:
            self.waiting = None

    async def _wait_stage_answer(
        self, runtime: StudyRuntime, rid: str, kind: str, seq: int, digest: str
    ) -> StageGateOutcome:
        while True:
            await runtime.ensure_lease(ttl_s=self._ttl_s)
            request = runtime.hitl[rid]
            if runtime.clock.now() >= request.deadline:
                # §2/§5: these stage kinds delegate silence to the critic. The
                # decision event mirrors ``_critic_decide`` (same idem key, so a
                # late runtime tick collapses into it); the plan-node delegate
                # mark is plan-kind machinery and does not apply to stage gates.
                await runtime.append_events([EventEnvelope(
                    study_id=request.study_id, type="HitlTimedOut", entity_key=rid,
                    idem_key=make_idem("HitlTimedOut", rid),
                    payload={"request_id": rid, "reason": "human_timeout", "spent_rub": 0.0},
                    ts=runtime.clock.now(), schema_version=writer_schema_version("HitlTimedOut"),
                )])
                study = runtime.study
                if study is None:
                    raise HitlRefuse("hitl_runtime_required")
                gate = study.hitl_policy.gate(kind)
                if gate.on_timeout in ("delegate", "delegate_capped"):
                    await runtime.append_events([EventEnvelope(
                        study_id=request.study_id, type="HitlDecided", entity_key=rid,
                        idem_key=make_idem("HitlDecided", rid, "critic", "timeout"),
                        payload={
                            "request_id": rid,
                            "decision": "accept",
                            "by": "policy",
                            "decided_by": "critic",
                            "approved_by": "critic",
                            "delegated_by": "timeout",
                            "reason": "timeout→agent (timeout)",
                            "spent_rub": 0.0,
                        },
                        ts=runtime.clock.now(), schema_version=writer_schema_version("HitlDecided"),
                    )])
                    return StageGateOutcome(decision="delegated", feedback=None)
                # fail/capped-less policy: the runtime's own timeout path owns
                # the task-failure effects; the run must not proceed.
                await runtime._hitl_timeout(runtime.hitl[rid])
                raise HitlRefuse("hitl_stage_timeout_failed")
            try:
                command = await asyncio.wait_for(self._queue.get(), timeout=self._poll_s)
            except TimeoutError:
                await runtime.ensure_lease(ttl_s=self._ttl_s)
            else:
                outcome = await self._apply_stage_answer(runtime, rid, kind, seq, digest, command)
                if outcome is not None:
                    return outcome

    async def _apply_stage_answer(
        self, runtime: StudyRuntime, rid: str, kind: str, seq: int, digest: str, command: _Answer
    ) -> StageGateOutcome | None:
        outcomes: list[StageGateOutcome | None] = []
        try:
            if command.body.opened_seq != seq or command.body.context_sha256 != digest:
                raise HitlRefuse("hitl_stale_context")
            if runtime.clock.now() >= runtime.hitl[rid].deadline:
                raise HitlRefuse("hitl_request_expired")
            await runtime.answer_hitl(rid, command.body.decision, feedback=command.body.feedback)
            decided = next(
                e for e in reversed(await runtime.store.read_all())
                if e.type == "HitlDecided" and e.payload["request_id"] == rid
            )
            command.done.set_result({
                "request_id": rid, "decision": decided.payload["decision"], "seq": decided.seq,
            })
            outcomes.append(StageGateOutcome(
                decision=command.body.decision, feedback=command.body.feedback,
            ))
        except HitlRefuse as exc:
            # Refusal is delivered on the HTTP future; stage gate stays open and wait continues.
            command.done.set_exception(exc)
            outcomes.append(None)
        finally:
            self._inflight = False
            if not command.done.done():
                command.done.set_exception(HitlRefuse("hitl_owner_closed"))
        return outcomes[0]

    async def _wait_answer(self, runtime: StudyRuntime, rid: str, seq: int, digest: str) -> bool:
        while True:
            await runtime.ensure_lease(ttl_s=self._ttl_s)
            request = runtime.hitl[rid]
            if runtime.clock.now() >= request.deadline:
                # Do not use experimental plan timeout delegation: interactive silence
                # is refusal, never implicit human approval.
                await runtime.append_events([EventEnvelope(
                    study_id=request.study_id, type="HitlTimedOut", entity_key=rid,
                    idem_key=make_idem("HitlTimedOut", rid),
                    payload={"request_id": rid, "reason": "human_timeout", "spent_rub": 0.0},
                    ts=runtime.clock.now(), schema_version=writer_schema_version("HitlTimedOut"),
                )])
                await runtime.cancel_study(by="hitl-plan-timeout")
                return False
            try:
                command = await asyncio.wait_for(self._queue.get(), timeout=self._poll_s)
            except TimeoutError:
                await runtime.ensure_lease(ttl_s=self._ttl_s)
            else:
                await self._apply_answer(runtime, rid, seq, digest, command)
                if command.done.done() and command.done.exception() is None:
                    return command.body.decision == "accept"

    async def _apply_answer(
        self, runtime: StudyRuntime, rid: str, seq: int, digest: str, command: _Answer,
    ) -> None:
        try:
            if command.body.opened_seq != seq or command.body.context_sha256 != digest:
                raise HitlRefuse("hitl_stale_context")
            if runtime.clock.now() >= runtime.hitl[rid].deadline:
                raise HitlRefuse("hitl_request_expired")
            if command.body.decision == "revise":
                # plan_approval keeps its accept/reject contract (hitl-map §2);
                # free-text revision is a stage-gate mechanic (HITL v2 §5).
                raise HitlRefuse("hitl_revise_needs_stage_gate")
            await runtime.answer_hitl(rid, command.body.decision, feedback=command.body.feedback)
            decided = next(
                e for e in reversed(await runtime.store.read_all())
                if e.type == "HitlDecided" and e.payload["request_id"] == rid
            )
            if command.body.decision == "reject":
                await runtime.cancel_study(by="human-plan-rejected")
            command.done.set_result({
                "request_id": rid, "decision": decided.payload["decision"], "seq": decided.seq,
            })
        except HitlRefuse as exc:
            command.done.set_exception(exc)
        finally:
            self._inflight = False
            if not command.done.done():
                command.done.set_exception(HitlRefuse("hitl_owner_closed"))
