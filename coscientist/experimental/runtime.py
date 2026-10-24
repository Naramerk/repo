"""StudyRuntime = M0 core + experimental HITL / costly / fork (pre-M2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import EventEnvelope, StoredEvent
from coscientist.core.execution.domain import HitlRequest
from coscientist.core.finalize.study_view import study_view
from coscientist.experimental.costly import DelegatedBudget
from coscientist.experimental.hitl import HitlForkMixin


class StudyRuntime(ApiStudyRuntime, HitlForkMixin):  # type: ignore[misc]
    """API lifecycle + core machine plus HITL gates, DelegatedBudget, fork stub.

    ApiStudyRuntime (machine before LifecycleMixin) keeps ``_append`` real;
    HitlForkMixin adds HITL/costly/fork without shadowing core append.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hitl = {}
        self._delegated_costly_spent: Decimal = Decimal("0")
        self.delegated_budget: DelegatedBudget | None = None
        self._pending_costly_events = {}
        self._hitl_notices = []

    def _project_local(self, ev: EventEnvelope | StoredEvent) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            super()._project_local(ev)
            if self.study and self.study.budget and self.study.budget.delegated_costly is not None:
                # Budget.delegated_costly is Decimal; DelegatedBudget requires float.
                self.delegated_budget = DelegatedBudget(
                    float(self.study.budget.delegated_costly)  # audit-ok: Decimal→float for DelegatedBudget wire
                )
            return
        if ev.type == "BudgetDebited":
            super()._project_local(ev)  # study spent for BudgetExceeded
            if p.get("budget"):
                self.delegated_budget = DelegatedBudget.from_state(p["budget"])
            if "spent_rub" not in p:
                raise JournalFormatError("BudgetDebited missing spent_rub")
            raw_spent = p["spent_rub"]
            if type(raw_spent) is float:
                spent = Decimal(str(raw_spent))  # audit-ok: float→Decimal via str (stdlib exactness)
            else:
                spent = Decimal(raw_spent)
            if spent > 0:
                self._delegated_costly_spent += spent
            return
        if ev.type == "HitlOpened":
            # Strip unknown keys (e.g. legacy proposal_id); HitlRequest is extra=forbid.
            fields = HitlRequest.model_fields
            clean = {k: v for k, v in p.items() if k in fields}
            self.hitl[p["request_id"]] = HitlRequest.model_validate(clean)
            # Core fold: _hitl_requests (expand delegated_by gate).
            super()._project_local(ev)
            return
        if ev.type in ("HitlDecided", "HitlTimedOut"):
            if p.get("keep_open") or p.get("effect") == "WAIT":
                self._hitl_notices.append(dict(p))
            else:
                h = self.hitl.get(p["request_id"])
                if h:
                    self.hitl[p["request_id"]] = h.model_copy(update={"open": False})
            if "spent_rub" in p:
                raw_spent = p["spent_rub"]
                if type(raw_spent) is float:
                    spent = Decimal(str(raw_spent))  # audit-ok: float→Decimal via str (stdlib exactness)
                else:
                    spent = Decimal(raw_spent)
                if spent > 0:
                    self._delegated_costly_spent += spent
            # Core fold: _hitl_decisions + close _hitl_requests (HitlDecided).
            # HitlTimedOut has no core fold — experimental UI state only.
            if ev.type == "HitlDecided":
                super()._project_local(ev)
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
        from coscientist.core.claims.dispositions import dispositions

        open_hitl = sum(1 for h in self.hitl.values() if h.open)
        # Prefer direct attribute; AttributeError → no lookup (structural absent).
        try:
            arts = self.artifacts
        except AttributeError:
            arts = None
        try:
            lookup = arts.get_meta if arts is not None else None
        except AttributeError:
            lookup = None
        try:
            results = self.results
        except AttributeError:
            results = None
        try:
            graph = self.graph
        except AttributeError:
            graph = None
        try:
            question_disposals = self.question_disposals
        except AttributeError:
            question_disposals = None
        try:
            journal_kinds = self.artifact_kinds
        except AttributeError:
            journal_kinds = None
        qdisp = (
            dispositions(
                tasks=self.tasks,
                plans=self.plans,
                graph=graph,
                study=self.study,
                results=results,
                artifact_lookup=lookup if callable(lookup) else None,
                question_disposals=question_disposals,
                journal_kinds=journal_kinds,
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
        # PR20: delegated proposed_check_review timeout ≡ reject (no PENDING hang).
        from coscientist.core.plan.proposed_check import reject_stale_proposed_check_reviews

        await reject_stale_proposed_check_reviews(self)
        await super().tick()
