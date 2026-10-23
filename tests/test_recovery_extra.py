"""Recovery / research-graph acceptance stubs for M0."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.execution.domain import TaskState
from coscientist.core.events.ids import result_id
from coscientist.core.execution.recover import recover
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


@pytest.mark.asyncio
async def test_recovery_stage_task_not_rerun(tmp_path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_rec", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_rec",
    )
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    assert result_id(aid) in rt.results or aid in rt.results
    n_attempts_before = len(rt.tasks["st_rec/SPEC"].attempts)
    await recover(rt)
    assert len(rt.tasks["st_rec/SPEC"].attempts) == n_attempts_before
    assert rt.tasks["st_rec/SPEC"].state == TaskState.DONE
    # no second AttemptStarted after result
    starts = [e for e in await rt.store.read_all() if e.type == "AttemptStarted"]
    assert len(starts) == 1
    await close_runtime(rt)


@pytest.mark.xfail(reason="RT1 plan↔task graph linking needs GraphBuilder (M1)", strict=False)
def test_rt1_task_linked_to_plan():
    raise NotImplementedError("GraphBuilder plans→experiment edges in M1")


@pytest.mark.xfail(reason="H1 hypothesis verdict needs GraphBuilder + §4.1 (M1)", strict=False)
def test_h1_hypothesis_gets_verdict():
    raise NotImplementedError("hypothesis verdict aggregation in M1")
