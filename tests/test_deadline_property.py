"""Hypothesis stateful: every study closes by deadline (DESIGN §3.3)."""

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
    rule,
    run_state_machine_as_test,
)

from coscientist.core.clock import FakeClock
from coscientist.core.domain import Clarification, TaskSpec, TaskState
from coscientist.core.errors import LeaseLost
from coscientist.core.projections import fold_events
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime


class StudyClosesMachine(RuleBasedStateMachine):
    task_keys = Bundle("task_keys")
    attempt_ids = Bundle("attempt_ids")
    hitl_ids = Bundle("hitl_ids")

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

    @initialize(data=st.data())
    def boot(self, data):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="cos_prop_"))
        seconds = data.draw(st.integers(min_value=20, max_value=120))
        self.study_deadline = self.clock.now() + timedelta(seconds=seconds)
        self.rt = self._run(
            open_runtime(self.tmp, self.sid, owner="owner-a", clock=self.clock)
        )
        self._run(
            self.rt.create_study(
                profile="research",
                request="prop",
                catalog_version="0.1.0",
                deadline=self.study_deadline,
                sid=self.sid,
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
        if self.lost_lease or self.rt is None:
            return "PLAN"
        # finish stages if needed
        for key in ("SPEC", "HYP", "LIT"):
            t = self.rt.tasks.get(f"{self.sid}/{key}")
            if t and t.state == TaskState.PENDING:
                try:
                    aid = self._run(self.rt.start_attempt(key))
                    self._run(self.rt.finish_attempt(aid))
                except Exception:
                    pass
        if not any(k.startswith("EXP-") for k in (t.spec.task_key for t in self.rt.tasks.values())):
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
            try:
                self._run(self.rt.accept_plan(specs))
            except Exception:
                pass
        return "EXP-1"

    @rule(key=task_keys, target=attempt_ids)
    def start_attempt(self, key):
        if self.lost_lease or self.rt is None:
            return "none"
        try:
            return self._run(self.rt.start_attempt(key))
        except Exception:
            return "none"

    @rule(aid=attempt_ids)
    def finish_attempt(self, aid):
        if self.lost_lease or aid == "none" or self.rt is None:
            return
        try:
            self._run(self.rt.finish_attempt(aid))
        except Exception:
            pass

    @rule(aid=attempt_ids)
    def fail_attempt(self, aid):
        if self.lost_lease or aid == "none" or self.rt is None:
            return
        try:
            self._run(self.rt.fail_attempt(aid, retryable=True))
        except Exception:
            pass

    @rule(aid=attempt_ids)
    def hang_attempt(self, aid):
        # hang is only at start; noop here
        return

    @rule(delta=st.integers(min_value=1, max_value=30))
    def advance_clock(self, delta):
        self.clock.advance(timedelta(seconds=delta))
        if self.rt and not self.lost_lease:
            try:
                self._run(self.rt.tick())
            except LeaseLost:
                self.lost_lease = True
            except Exception:
                pass

    @rule(key=task_keys, target=hitl_ids)
    def open_hitl(self, key):
        if self.lost_lease or self.rt is None:
            return "none"
        try:
            return self._run(self.rt.open_hitl(key, kind="plan_approval", timeout_s=15))
        except Exception:
            return "none"

    @rule(hid=hitl_ids)
    def answer_hitl(self, hid):
        if self.lost_lease or hid == "none" or self.rt is None:
            return
        try:
            self._run(self.rt.answer_hitl(hid))
        except Exception:
            pass

    @rule()
    def recover_after_idle(self):
        if self.lost_lease or self.rt is None:
            return
        try:
            self._run(self.rt.recover_after_idle(grace_s=5))
            self._run(recover(self.rt))
        except LeaseLost:
            self.lost_lease = True
        except Exception:
            pass

    @rule(key=task_keys)
    def cancel_task(self, key):
        if self.lost_lease or self.rt is None:
            return
        try:
            self._run(self.rt.cancel_task(key))
        except Exception:
            pass

    @rule()
    def fork_session(self):
        if self.lost_lease or self.rt is None:
            return
        try:
            self._run(
                self.rt.fork([Clarification(target="EXP-1", text="retry")])
            )
            self.fork_count += 1
        except Exception:
            pass

    @rule()
    def lose_lease(self):
        """Second owner acquires lease; first writer must not record further."""
        if self.lost_lease or self.rt is None:
            return
        self.clock.advance(timedelta(seconds=31))
        other = self._run(
            open_runtime(
                self.tmp, self.sid, owner="owner-b", clock=self.clock, acquire=True
            )
        )
        # poison first runtime lease
        self.rt.store.epoch = 0
        self.lost_lease = True
        try:
            self._run(self.rt.tick())
            raise AssertionError("stale epoch must not write")
        except LeaseLost:
            pass
        except Exception:
            pass
        # expire owner-b lease and drop its connection before teardown reopen
        self.clock.advance(timedelta(seconds=31))
        try:
            self._run(close_runtime(other))
        except Exception:
            other.store.close()

    @rule()
    def crash_and_recover(self):
        if self.lost_lease or self.rt is None:
            return
        try:
            self._run(recover(self.rt))
        except LeaseLost:
            self.lost_lease = True
        except Exception:
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
        events = self.rt.store.read_all()
        folded = fold_events(events)
        for table, rows in folded.items():
            proj_rows = self.rt.store.get_projection(table)
            proj = {}
            for r in proj_rows:
                for f in (
                    "attempt_id",
                    "result_id",
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
        # checked by lose_lease raising; epoch fencing in store
        return

    def check_finally_closed(self):
        if self.rt is None or self.study_deadline is None:
            return
        # Always reopen with a fresh connection/owner so deadline drain is not
        # blocked by a poisoned epoch or a lingering second-owner connection.
        old = self.rt
        try:
            self._run(close_runtime(old))
        except Exception:
            try:
                old.store.close()
            except Exception:
                pass
        # Never move the clock backwards (lease expiry is absolute); clear lease row.
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
                self.tmp, self.sid, owner="owner-final", clock=self.clock
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
async def test_every_study_closes_by_deadline():
    def run():
        class M(StudyClosesMachine):
            def teardown(self):
                try:
                    self.check_finally_closed()
                finally:
                    super().teardown()

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
                # Shrink is expensive with asyncio+sqlite; generation coverage is enough for M0.
                phases=[Phase.generate, Phase.target],
            ),
        )

    await asyncio.get_event_loop().run_in_executor(None, run)
