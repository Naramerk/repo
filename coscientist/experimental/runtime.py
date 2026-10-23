"""StudyRuntime = M0 core + experimental HITL / costly / fork (pre-M2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coscientist.core.domain import HitlRequest, TaskState
from coscientist.core.events import EventEnvelope
from coscientist.core.machine import StudyRuntime as CoreStudyRuntime
from coscientist.core.study_view import study_view
from coscientist.experimental.costly import DelegatedBudget
from coscientist.experimental.hitl import HitlForkMixin


class StudyRuntime(CoreStudyRuntime, HitlForkMixin):
    """Core state machine plus HITL gates, DelegatedBudget, and fork stub.

    Core is listed first so ``_append`` / ``cancel_task`` resolve to the real
    implementations rather than mixin ``NotImplementedError`` stubs.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hitl: dict[str, HitlRequest] = {}
        self._delegated_costly_spent: float = 0.0
        self.delegated_budget: DelegatedBudget | None = None
        self._pending_costly_events: dict[str, dict[str, Any]] = {}
        self._hitl_notices: list[dict[str, Any]] = []

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            super()._project_local(ev)
            if self.study and self.study.budget and self.study.budget.delegated_costly is not None:
                self.delegated_budget = DelegatedBudget(
                    float(self.study.budget.delegated_costly)
                )
            return
        if ev.type == "BudgetDebited":
            if p.get("budget"):
                self.delegated_budget = DelegatedBudget.from_state(p["budget"])
            spent = float(p.get("spent_rub") or 0.0)
            if spent > 0:
                self._delegated_costly_spent += spent
            return
        if ev.type == "HitlOpened":
            self.hitl[p["request_id"]] = HitlRequest.model_validate(p)
            return
        if ev.type in ("HitlDecided", "HitlTimedOut"):
            if p.get("keep_open") or p.get("effect") == "WAIT":
                self._hitl_notices.append(dict(p))
            else:
                h = self.hitl.get(p["request_id"])
                if h:
                    self.hitl[p["request_id"]] = h.model_copy(update={"open": False})
            spent = float(p.get("spent_rub") or 0.0)
            if spent > 0:
                self._delegated_costly_spent += spent
            return
        if ev.type == "HitlDeadlineExtended":
            h = self.hitl.get(p["request_id"])
            if h and "deadline" in p:
                dl = p["deadline"]
                if isinstance(dl, str):
                    dl = datetime.fromisoformat(dl)
                self.hitl[p["request_id"]] = h.model_copy(update={"deadline": dl})
            return
        super()._project_local(ev)

    def view(self):
        open_hitl = sum(1 for h in self.hitl.values() if h.open)
        sim = sum(
            1
            for t in self.tasks.values()
            if t.state == TaskState.DONE
            and any(
                self.attempts.get(a, {}).get("provenance", "real") != "real"
                for a in t.attempts
            )
        )
        return study_view(self.tasks.values(), open_hitl=open_hitl, simulated_done=sim)

    def invariant_deadlines(self) -> bool:
        if not super().invariant_deadlines():
            return False
        if self.study is None:
            return True
        sd = self.study.deadline
        for h in self.hitl.values():
            if h.deadline > sd:
                return False
        return True

    async def tick(self) -> None:
        if self.study is None or self._lease_lost:
            return
        await self.ensure_lease()
        now = self.clock.now()
        for _hid, h in list(self.hitl.items()):
            if h.open and now > h.deadline:
                await self._hitl_timeout(h)
        await super().tick()
