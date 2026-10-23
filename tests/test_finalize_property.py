"""PR 3: Hypothesis stateful finalize property + future xfail stubs."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from coscientist.core.clock import FakeClock
from coscientist.core.domain import QuestionSpec, TaskSpec, TaskState
from coscientist.core.errors import (
    AttemptClosedError,
    HitlUnsupported,
    LeaseLost,
    StudyClosedError,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize_nodes import conclusion_id_for_question
from coscientist.core.inputs import InputUnavailable
from coscientist.core.projections import fold_events
from coscientist.core.question_tasks import known_questions
from coscientist.core.reasons import TaskReason
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime

_RULE_ERR = (
    KeyError,
    ValueError,
    StudyClosedError,
    AttemptClosedError,
    InputUnavailable,
    HitlUnsupported,
)
_TRIGGER_TERM = {
    "cancel": "cancelled",
    "budget": "budget",
    "deadline": "deadline",
    "intake": "refused",
}


class FinalizePropertyMachine(RuleBasedStateMachine):
    """create/accept/cancel/attempts/clock/crash/downtime/lease → always finalize."""

    task_keys = Bundle("task_keys")
    attempt_ids = Bundle("attempt_ids")
    runtime_cls: type

    def __init__(self):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.clock = FakeClock()
        self.tmp = Path("/tmp/cos_fin_prop")
        self.rt = None
        self.sid = "st_fprop"
        self.study_deadline = None
        self.lost_lease = False
        self._plan_rev = 0

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _finalized(self) -> bool:
        return bool(self.rt is not None and getattr(self.rt, "_finalized", False))

    def _writable(self) -> bool:
        return self.rt is not None and not self.lost_lease and not self._finalized()

    def _call(self, coro, default=None):
        try:
            return self._run(coro)
        except LeaseLost:
            self.lost_lease = True
            return default
        except _RULE_ERR:
            return default

    def _call_attempt(self, coro, default=None):
        """Attempt API still raises RuntimeError for invalid transitions."""
        try:
            return self._call(coro, default=default)
        except RuntimeError:
            return default

    def _fin_ev(self):
        if self.rt is None:
            return None
        fins = [e for e in self._run(self.rt.store.read_all()) if e.type == "StudyFinalized"]
        return fins[0] if fins else None

    @initialize(data=st.data())
    def boot(self, data):
        self.tmp = Path(tempfile.mkdtemp(prefix="cos_fprop_"))
        seconds = data.draw(st.integers(min_value=25, max_value=120))
        self.study_deadline = self.clock.now() + timedelta(seconds=seconds)
        self.rt = self._run(
            open_runtime(
                self.tmp, self.sid, owner="owner-a", clock=self.clock,
                runtime_cls=self.runtime_cls,
            )
        )
        self._run(
            self.rt.create_study(
                profile="research", request="prop", catalog_version="0.1.0",
                deadline=self.study_deadline, sid=self.sid, finalize_reserve_s=5,
            )
        )

    def teardown(self):
        if self.rt is not None:
            try:
                self._run(close_runtime(self.rt))
            except Exception:
                pass
        self._loop.close()
        if self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)

    @rule(target=task_keys, data=st.data())
    def accept_plan(self, data):
        if not self._writable():
            return "PLAN"
        for key in ("SPEC", "HYP", "LIT"):
            t = self.rt.tasks.get(f"{self.sid}/{key}")
            if t and t.state == TaskState.PENDING:
                self._call_attempt(self.rt.start_attempt(key))
                self._call(self.rt.cancel_task(key, reason="cancelled"))
        self._plan_rev += 1
        rev = self._plan_rev
        n = data.draw(st.integers(min_value=1, max_value=12))
        fan = data.draw(st.integers(min_value=1, max_value=min(10, n)))
        n_q = data.draw(st.integers(min_value=1, max_value=3))
        qs = [QuestionSpec(id=f"Q{rev}_{i}", text=f"q{rev}_{i}") for i in range(n_q)]
        specs = []
        for i in range(n):
            key = f"EXP-r{rev}-{i + 1}"
            deps = ["PLAN"] if i == 0 else [f"EXP-r{rev}-{max(1, i - fan + 1)}"]
            addr = [qs[i % len(qs)].id]
            specs.append(
                TaskSpec(
                    task_key=key, title=key, goal="g", route="experiment",
                    depends_on=deps, addresses=addr,
                )
            )
        self._call(self.rt.accept_plan(specs, questions=qs, revision=rev))
        return f"EXP-r{rev}-1"

    @rule()
    def cancel_study(self):
        if not self._writable():
            return
        self._call(self.rt.cancel_study(by="prop"))

    @rule(key=task_keys, target=attempt_ids)
    def start_attempt(self, key):
        if not self._writable():
            return "none"
        return self._call_attempt(self.rt.start_attempt(key), default="none") or "none"

    @rule(aid=attempt_ids)
    def finish_attempt(self, aid):
        if not self._writable() or aid == "none":
            return
        att = self.rt.attempts.get(aid, {})
        tid = att.get("task_id")
        if tid:
            self._call(self.rt.cancel_task(tid, reason="cancelled"))

    @rule(aid=attempt_ids)
    def fail_attempt(self, aid):
        if not self._writable() or aid == "none":
            return
        self._call_attempt(self.rt.fail_attempt(aid, retryable=True))

    @rule(key=task_keys, target=attempt_ids)
    def hang_attempt(self, key):
        """Start an attempt and leave it open (no finish/fail in this rule)."""
        if not self._writable():
            return "none"
        return self._call_attempt(self.rt.start_attempt(key), default="none") or "none"

    @rule(delta=st.integers(min_value=1, max_value=40))
    def advance_clock(self, delta):
        self.clock.advance(timedelta(seconds=delta))
        if self._writable():
            self._call(self.rt.tick())

    @rule()
    def long_downtime(self):
        if self.rt is None or self.study_deadline is None:
            return
        self.clock.set(self.study_deadline + timedelta(hours=2))
        if self.lost_lease:
            return
        self._call(self.rt.tick())

    @rule()
    def crash_and_recover(self):
        if self.rt is None or self.lost_lease:
            return
        try:
            self._run(recover(self.rt))
        except LeaseLost:
            self.lost_lease = True
        except _RULE_ERR:
            pass

    @rule()
    def lose_lease(self):
        if self.lost_lease or self.rt is None:
            return
        self.clock.advance(timedelta(seconds=400))
        other = self._run(
            open_runtime(
                self.tmp, self.sid, owner="owner-b", clock=self.clock,
                acquire=True, runtime_cls=self.runtime_cls,
            )
        )
        self.rt.store.epoch = 0
        self.lost_lease = True
        try:
            self._run(self.rt.tick())
            raise AssertionError("stale epoch must not write")
        except LeaseLost:
            pass
        self.clock.advance(timedelta(seconds=400))
        try:
            self._run(close_runtime(other))
        except (LeaseLost, OSError):
            self._run(other.store.close())

    @invariant()
    def inv_closed_iff_finalized(self):
        if self.rt is None or self.lost_lease:
            return
        fin = self._finalized()
        has = self._fin_ev() is not None
        assert fin == has
        assert (self.rt.view().phase == "closed") == fin

    @invariant()
    def inv_store_rejects_after_finalized(self):
        if self.rt is None or self.lost_lease or not self._finalized():
            return
        try:
            self._run(self.rt.ensure_lease(ttl_s=300))
        except LeaseLost:
            self.lost_lease = True
            return
        n = len(self._run(self.rt.store.read_all()))
        with pytest.raises(StudyClosedError):
            self._run(
                self.rt.store.append(
                    EventEnvelope(
                        study_id=self.sid, type="TaskDone", entity_key="late",
                        idem_key=make_idem("TaskDone", "late", str(n)),
                        payload={
                            "task_id": "late", "outcome": "FAILED", "reason": "failed",
                        },
                        ts=self.clock.now(), schema_version=2,
                    )
                )
            )
        assert len(self._run(self.rt.store.read_all())) == n

    @invariant()
    def inv_deadlines_le_effective(self):
        if self.rt is None:
            return
        assert self.rt.invariant_deadlines()

    @invariant()
    def inv_open_attempts_le_1(self):
        if self.rt is None:
            return
        assert self.rt.invariant_open_attempts()

    @invariant()
    def inv_replay_equals_projection(self):
        if self.rt is None or self.lost_lease:
            return
        events = self._run(self.rt.store.read_all())
        folded = fold_events(events)
        for table, rows in folded.items():
            proj_rows = self._run(self.rt.store.get_projection(table))
            proj = {}
            for r in proj_rows:
                for f in (
                    "result_id", "attempt_id", "request_id", "node_id", "edge_id",
                    "plan_id", "task_id", "id", "study_id",
                ):
                    if f in r:
                        proj[str(r[f])] = r
                        break
            for key in rows:
                assert key in proj

    def check_finally(self):
        if self.rt is None or self.study_deadline is None:
            return
        old = self.rt
        try:
            self._run(close_runtime(old))
        except Exception:
            try:
                self._run(old.store.close())
            except Exception:
                pass
        # Expire any held lease by clock (no raw sqlite DELETE).
        target = max(self.clock.now(), self.study_deadline) + timedelta(seconds=400)
        self.clock.set(target)
        self.rt = self._run(
            open_runtime(
                self.tmp, self.sid, owner="owner-final", clock=self.clock,
                runtime_cls=self.runtime_cls,
            )
        )
        self.lost_lease = False
        self._run(self.rt.ensure_lease(ttl_s=300))
        self._run(self.rt.tick())
        self._run(self.rt.tick())
        self.final_has_study_finalized()
        self.final_has_report_or_minimal()
        self.final_terminal_reason_matches_trigger()
        self.final_every_question_has_disposition()
        self.final_i2_conclusion_or_minimal_coverage()
        self.final_every_task_reason_in_enum()

    def final_has_study_finalized(self):
        assert self._fin_ev() is not None
        assert self.rt.view().phase == "closed"

    def final_has_report_or_minimal(self):
        p = self._fin_ev().payload
        assert p.get("report_artifact_id") or p.get("minimal_report")

    def final_terminal_reason_matches_trigger(self):
        p = self._fin_ev().payload
        trig, term = p["trigger"], p["terminal_reason"]
        if trig == "all_done":
            assert term in ("completed", "human_timeout")
        else:
            assert term == _TRIGGER_TERM[trig]

    def final_every_question_has_disposition(self):
        p = self._fin_ev().payload
        qdisp = p.get("question_dispositions") or {}
        for qid in known_questions(self.rt.plans):
            assert qid in qdisp, qid
            assert qdisp[qid].get("disposition")
            assert qdisp[qid].get("reason") is not None

    def final_i2_conclusion_or_minimal_coverage(self):
        p = self._fin_ev().payload
        known = known_questions(self.rt.plans)
        minimal = p.get("minimal_report") or {}
        mq = {q.get("question_id") for q in (minimal.get("questions") or [])}
        truncated = bool(minimal.get("truncated"))
        summary = str(minimal.get("summary") or "")
        m = re.search(r"вопросов (\d+)", summary)
        summary_n = int(m.group(1)) if m else -1
        conclusions = {n.node_id for n in self.rt.graph.nodes_by_kind("conclusion")}
        for qid in known:
            cid = conclusion_id_for_question(self.sid, qid)
            if cid in conclusions or qid in mq:
                continue
            # Truncated: structural count in summary covers all known questions.
            if truncated and summary_n >= len(known):
                continue
            raise AssertionError(f"I2 uncovered question {qid}")

    def final_every_task_reason_in_enum(self):
        for t in self.rt.tasks.values():
            assert t.state == TaskState.DONE
            if t.reason is None:
                continue
            rv = t.reason.value if isinstance(t.reason, TaskReason) else str(t.reason)
            assert rv in {r.value for r in TaskReason}


_HYP = settings(
    max_examples=25,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    phases=[Phase.generate, Phase.target],
)


@pytest.mark.asyncio
async def test_finalize_property(runtime_cls):
    def run():
        class M(FinalizePropertyMachine):
            def teardown(self):
                try:
                    self.check_finally()
                finally:
                    super().teardown()

        M.runtime_cls = runtime_cls
        run_state_machine_as_test(M, settings=_HYP)

    await asyncio.get_event_loop().run_in_executor(None, run)
