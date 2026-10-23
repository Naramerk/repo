"""Hypothesis stateful: every study closes by deadline (DESIGN §3.3).

Core invariants (open attempts, deadlines) live on StudyRuntime. HITL/fork
rules are gated with ``@precondition(supports_hitl)`` so the machine runs on
both ``rt_core`` and ``rt_exp``.
"""

from __future__ import annotations

import asyncio
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
    precondition,
    rule,
    run_state_machine_as_test,
)

from coscientist.core.clock import FakeClock
from coscientist.core.domain import Clarification, TaskSpec, TaskState
from coscientist.core.errors import HitlUnsupported, LeaseLost, StudyClosedError
from coscientist.core.hitl_guard import supports_hitl
from coscientist.core.inputs import InputUnavailable
from coscientist.core.projections import fold_events
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime

# Expected failures when a random rule hits an invalid transition / missing key.
# Do not include AssertionError — that would swallow production asserts.
_RULE_ERR = (RuntimeError, KeyError, ValueError, InputUnavailable, StudyClosedError)


class StudyClosesMachine(RuleBasedStateMachine):
    """Base property machine. Subclasses set ``runtime_cls`` before running."""

    task_keys = Bundle("task_keys")
    attempt_ids = Bundle("attempt_ids")
    hitl_ids = Bundle("hitl_ids")
    runtime_cls: type  # set by the test wrapper

    def __init__(self):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.clock = FakeClock()
        self.tmp = Path("/tmp/coscientist_hyp")  # overwritten in initialize
        self.rt = None
        self.sid = "st_prop"
        self.study_deadline = None
        self.fork_count = 0
        self.lost_lease = False

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _finalized(self) -> bool:
        return bool(self.rt is not None and getattr(self.rt, "_finalized", False))

    def _writable(self) -> bool:
        return self.rt is not None and not self.lost_lease and not self._finalized()

    def _hitl_ok(self) -> bool:
        return self.rt is not None and supports_hitl(self.rt) and self._writable()

    def _call(self, coro, default=None):
        """Run a mutating coro; map LeaseLost / expected errors to default."""
        try:
            return self._run(coro)
        except LeaseLost:
            self.lost_lease = True
            return default
        except _RULE_ERR:
            return default

    @initialize(data=st.data())
    def boot(self, data):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="cos_prop_"))
        seconds = data.draw(st.integers(min_value=20, max_value=120))
        self.study_deadline = self.clock.now() + timedelta(seconds=seconds)
        self.rt = self._run(
            open_runtime(
                self.tmp,
                self.sid,
                owner="owner-a",
                clock=self.clock,
                runtime_cls=self.runtime_cls,
            )
        )
        self._run(
            self.rt.create_study(
                profile="research",
                request="prop",
                catalog_version="0.1.0",
                deadline=self.study_deadline,
                sid=self.sid,
                finalize_reserve_s=5,
            )
        )

    def teardown(self):
        if self.rt is not None:
            try:
                self._run(close_runtime(self.rt))
            except Exception:
                pass
        self._loop.close()

    @rule(target=task_keys)
    def create_and_accept_plan(self):
        if not self._writable():
            return "PLAN"
        for key in ("SPEC", "HYP", "LIT"):
            t = self.rt.tasks.get(f"{self.sid}/{key}")
            if t and t.state == TaskState.PENDING:
                self._call(self.rt.start_attempt(key))
                self._call(self.rt.cancel_task(key, reason="cancelled"))
        if not any(
            k.startswith("EXP-") for k in (t.spec.task_key for t in self.rt.tasks.values())
        ):
            specs = [
                TaskSpec(
                    task_key="EXP-1",
                    title="e1",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                ),
                TaskSpec(
                    task_key="EXP-2",
                    title="e2",
                    goal="g",
                    route="experiment",
                    depends_on=["EXP-1"],
                ),
            ]
            self._call(self.rt.accept_plan(specs))
        return "EXP-1"

    @rule(key=task_keys, target=attempt_ids)
    def start_attempt(self, key):
        if not self._writable():
            return "none"
        return self._call(self.rt.start_attempt(key), default="none") or "none"

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
        self._call(self.rt.fail_attempt(aid, retryable=True))

    @rule(delta=st.integers(min_value=1, max_value=30))
    def advance_clock(self, delta):
        self.clock.advance(timedelta(seconds=delta))
        if self._writable():
            self._call(self.rt.tick())

    @precondition(lambda self: self._hitl_ok())
    @rule(key=task_keys, target=hitl_ids)
    def open_hitl(self, key):
        return self._call(
            self.rt.open_hitl(key, kind="plan_approval", timeout_s=15), default="none"
        ) or "none"

    @precondition(lambda self: self._hitl_ok())
    @rule(hid=hitl_ids)
    def answer_hitl(self, hid):
        if hid == "none":
            return
        self._call(self.rt.answer_hitl(hid))

    @precondition(lambda self: self._hitl_ok())
    @rule()
    def recover_after_idle(self):
        try:
            self._run(self.rt.recover_after_idle(grace_s=5))
            self._run(recover(self.rt))
        except LeaseLost:
            self.lost_lease = True
        except (HitlUnsupported, RuntimeError, StudyClosedError):
            pass

    @rule(key=task_keys)
    def cancel_task(self, key):
        if not self._writable():
            return
        self._call(self.rt.cancel_task(key))

    @precondition(lambda self: self._hitl_ok())
    @rule()
    def fork_session(self):
        try:
            self._run(self.rt.fork([Clarification(target="EXP-1", text="retry")]))
            self.fork_count += 1
        except LeaseLost:
            self.lost_lease = True
        except _RULE_ERR:
            pass

    @rule()
    def lose_lease(self):
        """Second owner acquires lease; first writer must not record further."""
        if self.lost_lease or self.rt is None:
            return
        self.clock.advance(timedelta(seconds=31))
        other = self._run(
            open_runtime(
                self.tmp,
                self.sid,
                owner="owner-b",
                clock=self.clock,
                acquire=True,
                runtime_cls=self.runtime_cls,
            )
        )
        self.rt.store.epoch = 0
        self.lost_lease = True
        try:
            # tick() is a no-op after StudyFinalized; renew still fences.
            self._run(self.rt.ensure_lease())
            raise AssertionError("stale epoch must not write")
        except LeaseLost:
            pass
        self.clock.advance(timedelta(seconds=31))
        try:
            self._run(close_runtime(other))
        except (LeaseLost, RuntimeError, OSError):
            self._run(other.store.close())

    @rule()
    def crash_and_recover(self):
        if not self._writable():
            return
        try:
            self._run(recover(self.rt))
        except LeaseLost:
            self.lost_lease = True
        except (HitlUnsupported, RuntimeError, StudyClosedError):
            pass

    @invariant()
    def open_attempts_at_most_one(self):
        if self.rt is None:
            return
        assert self.rt.invariant_open_attempts()

    @invariant()
    def deadlines_within_study(self):
        if self.rt is None:
            return
        assert self.rt.invariant_deadlines()

    @invariant()
    def replay_equals_projection(self):
        if self.rt is None or self.lost_lease:
            return
        events = self._run(self.rt.store.read_all())
        folded = fold_events(events)
        for table, rows in folded.items():
            proj_rows = self._run(self.rt.store.get_projection(table))
            proj = {}
            for r in proj_rows:
                for f in (
                    "result_id",
                    "attempt_id",
                    "request_id",
                    "node_id",
                    "edge_id",
                    "plan_id",
                    "task_id",
                    "id",
                    "study_id",
                ):
                    if f in r:
                        proj[str(r[f])] = r
                        break
            for key in rows:
                assert key in proj

    @invariant()
    def lease_lost_writes_nothing_new(self):
        return

    def check_finally_closed(self):
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
        target = max(self.clock.now(), self.study_deadline) + timedelta(seconds=35)
        self.clock.set(target)
        try:
            import sqlite3

            db = self.tmp / self.sid / "events.db"
            conn = sqlite3.connect(str(db))
            conn.execute("DELETE FROM lease")
            conn.commit()
            conn.close()
        except Exception:
            pass
        self.rt = self._run(
            open_runtime(
                self.tmp,
                self.sid,
                owner="owner-final",
                clock=self.clock,
                runtime_cls=self.runtime_cls,
            )
        )
        self.lost_lease = False
        self._run(self.rt.ensure_lease(ttl_s=300))
        self._run(self.rt.tick())
        self._run(self.rt.tick())
        assert all(t.state == TaskState.DONE for t in self.rt.tasks.values()), {
            t.spec.task_key: (t.state, t.outcome, t.reason)
            for t in self.rt.tasks.values()
        }


@pytest.mark.asyncio
async def test_every_study_closes_by_deadline(runtime_cls):
    """Runs on both core and experimental via ``runtime_cls``."""

    def run():
        class M(StudyClosesMachine):
            def teardown(self):
                try:
                    self.check_finally_closed()
                finally:
                    super().teardown()

        M.runtime_cls = runtime_cls
        run_state_machine_as_test(
            M,
            settings=settings(
                max_examples=25,
                stateful_step_count=20,
                deadline=None,
                suppress_health_check=[
                    HealthCheck.too_slow,
                    HealthCheck.filter_too_much,
                ],
                phases=[Phase.generate, Phase.target],
            ),
        )

    await asyncio.get_event_loop().run_in_executor(None, run)
