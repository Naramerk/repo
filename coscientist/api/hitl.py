"""HITL commands are consumed by the lease-owning run, never another writer."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

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

    decision: Literal["accept", "reject"]
    opened_seq: StrictInt = Field(gt=0)
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass
class _Answer:
    body: AnswerBody
    done: asyncio.Future[dict[str, object]]


class HitlOwner:
    """One bounded inbox for one API run; the run coroutine owns all effects."""

    runtime_cls = StudyRuntime

    def __init__(self, *, poll_s: float, lease_ttl_s: float) -> None:
        self._poll_s = poll_s
        self._ttl_s = lease_ttl_s
        self._queue: asyncio.Queue[_Answer] = asyncio.Queue(maxsize=1)
        self.waiting: tuple[str, str] | None = None
        self._closed = False
        self._inflight = False

    def available(self, sid: str, rid: str) -> bool:
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
        self.waiting = (sid, rid)
        try:
            return await self._wait_answer(runtime, rid, opening.seq, prompt.context_sha256)
        finally:
            self.waiting = None

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
            await runtime.answer_hitl(rid, command.body.decision)
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
            self._inflight = False
        finally:
            if not command.done.done():
                command.done.set_exception(HitlRefuse("hitl_owner_closed"))
