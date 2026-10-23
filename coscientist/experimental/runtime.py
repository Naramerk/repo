"""StudyRuntime = M0 core + experimental HITL / costly / fork (pre-M2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.domain import HitlRequest
from coscientist.core.events import EventEnvelope
from coscientist.core.machine import StudyRuntime as CoreStudyRuntime
from coscientist.core.study_view import study_view
from coscientist.experimental.costly import DelegatedBudget
from coscientist.experimental.hitl import HitlForkMixin


class StudyRuntime(CoreStudyRuntime, HitlForkMixin):
    """Core state machine plus HITL gates, DelegatedBudget, and fork stub.

    Core is listed first so ``_append`` / ``cancel_task`` resolve to the real
    implementations rather than mixin Protocol/`...` stubs.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hitl: dict[str, HitlRequest] = {}
        self._delegated_costly_spent: Decimal = Decimal("0")
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
            super()._project_local(ev)  # study spent for BudgetExceeded
            if p.get("budget"):
                self.delegated_budget = DelegatedBudget.from_state(p["budget"])
            spent = Decimal(str(p.get("spent_rub") or 0))
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
            spent = Decimal(str(p.get("spent_rub") or 0))
            if spent > 0:
                self._delegated_costly_spent += spent
            if ev.type == "HitlDecided":
                rid = str(p.get("request_id") or ev.entity_key)
                decisions = getattr(self, "_hitl_decisions", None)
                if not isinstance(decisions, dict):
                    self._hitl_decisions = {}
                    decisions = self._hitl_decisions
                decisions[rid] = dict(p)
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
        from coscientist.core.dispositions import dispositions

        open_hitl = sum(1 for h in self.hitl.values() if h.open)
        arts = getattr(self, "artifacts", None)
        lookup = getattr(arts, "get_meta", None) if arts is not None else None
        qdisp = (
            dispositions(
                tasks=self.tasks,
                plans=self.plans,
                graph=getattr(self, "graph", None),
                study=self.study,
                results=getattr(self, "results", None),
                artifact_lookup=lookup if callable(lookup) else None,
            )
            if self.plans
            else None
        )
        ed = self.study.effective_deadline() if self.study else None
        return study_view(
            self.tasks.values(),
            open_hitl=open_hitl,
            attempts=self.attempts,
            finalized=self._finalized,
            now=self.clock.now(),
            effective_deadline=ed,
            question_dispositions=qdisp,
        )

    def invariant_deadlines(self) -> bool:
        if not super().invariant_deadlines():
            return False
        if self.study is None:
            return True
        ed = self.study.effective_deadline()
        for h in self.hitl.values():
            if h.deadline > ed:
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
