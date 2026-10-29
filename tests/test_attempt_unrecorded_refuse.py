"""AttemptUnrecorded: consumers match + typed refuse (NeedUnrecorded-style)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest

from coscientist.core.events.attempt_fields import (
    AttemptUnrecordedRefuse,
    attempt_closed,
    project_attempt_started_payload,
    require_attempt_bool,
    require_attempt_str,
)
from coscientist.core.events.events import StoredEvent, make_idem
from coscientist.core.events.ids import result_id
from coscientist.core.events.payload_wire import AttemptUnrecorded
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import ATTEMPT_STARTED_UNRECORDED_SINCE
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Provenance,
    RequirementRef,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.executor import Executor
from coscientist.core.execution.machine import new_owner
from coscientist.tools.effects import EffectIndex
from tests.helpers import (
    attach_executor,
    close_runtime,
    open_runtime,
    record_success,
)

_UNREC = {
    "kind": "unrecorded",
    "source_schema_version": ATTEMPT_STARTED_UNRECORDED_SINCE,
}


def test_require_attempt_bool_refuses_unrecorded_model() -> None:
    with pytest.raises(
        AttemptUnrecordedRefuse,
        match=r"attempt_field_unrecorded:closed:source_schema_version=",
    ) as ei:
        require_attempt_bool(
            AttemptUnrecorded(
                kind="unrecorded",
                source_schema_version=ATTEMPT_STARTED_UNRECORDED_SINCE,
            ),
            field="closed",
        )
    assert ei.value.reason == "attempt_field_unrecorded"
    assert ei.value.field == "closed"


def test_require_attempt_bool_refuses_unrecorded_dict() -> None:
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:closed"):
        require_attempt_bool(dict(_UNREC), field="closed")


def test_require_attempt_str_refuses_unrecorded() -> None:
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:study_id"):
        require_attempt_str(dict(_UNREC), field="study_id")


def test_attempt_closed_refuses_unrecorded() -> None:
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:closed"):
        attempt_closed({"closed": dict(_UNREC)})


def test_project_attempt_started_keeps_unrecorded_marker() -> None:
    out = project_attempt_started_payload(
        {
            "attempt_id": "a1",
            "closed": dict(_UNREC),
            "study_id": dict(_UNREC),
            "provenance": "real",
            "n": 1,
            "dispatch_id": "d",
            "route_returned": False,
            "task_id": "t",
        }
    )
    assert out["closed"] == _UNREC
    assert out["study_id"] == _UNREC
    assert out["provenance"] == "real"


def test_projection_attempt_started_stores_unrecorded_not_bool() -> None:
    from coscientist.core.events.projections import apply_to_map

    proj: dict[str, object] = {}
    ev = StoredEvent(
        study_id="st",
        type="AttemptStarted",
        entity_key="att1",
        idem_key=make_idem("AttemptStarted", "att1"),
        payload={
            "attempt_id": "att1",
            "n": 1,
            "dispatch_id": "d",
            "route_returned": False,
            "closed": dict(_UNREC),
            "provenance": "real",
            "task_id": "st/E1",
            "study_id": "st",
            "inputs": [],
            "tainted_context": False,
            "limitations": [],
            "attempt_role": "agent",
        },
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        schema_version=10,
        seq=1,
        epoch=1,
        payload_digest="x" * 64,
    )
    apply_to_map(proj, ev)
    row = proj["attempts"]["att1"]
    assert row["closed"] == _UNREC
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:closed"):
        attempt_closed(row)


@pytest.mark.asyncio
async def test_executor_mark_route_returned_refuses_unrecorded_closed(tmp_path, clock) -> None:
    from datetime import timedelta

    rt = await open_runtime(tmp_path, "st_unrec_closed", clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=300),
        sid="st_unrec_closed",
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    # Inject attempt row with unrecorded closed (bypass writer forever-field completeness).
    rt.attempts["att_x"] = {
        "attempt_id": "att_x",
        "closed": dict(_UNREC),
        "study_id": "st_unrec_closed",
        "task_id": "st_unrec_closed/E1",
        "provenance": "real",
        "inputs": [],
    }
    try:
        with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:closed"):
            await rt.executor.mark_route_returned("att_x", AgentTaskOutput(claim=None))
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_executor_record_refuses_unrecorded_study_id(tmp_path, clock) -> None:
    from datetime import timedelta

    rt = await open_runtime(tmp_path, "st_unrec_sid", clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=300),
        sid="st_unrec_sid",
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    rt.attempts["att_y"] = {
        "attempt_id": "att_y",
        "closed": False,
        "study_id": dict(_UNREC),
        "task_id": "st_unrec_sid/E1",
        "provenance": "real",
        "inputs": [],
    }
    try:
        with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:study_id"):
            await rt.executor.record("att_y", AgentTaskOutput(claim=None))
    finally:
        await close_runtime(rt)


def test_effects_fingerprint_refuses_unrecorded_study_id() -> None:
    idx = EffectIndex()
    idx.note_intended(
        "k1",
        {
            "study_id": dict(_UNREC),
            "task_key": "E1",
            "tool": "t",
            "args_digest": "d",
        },
    )
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:study_id"):
        idx.in_doubt_for_fingerprint(
            study_id="st",
            task_key="E1",
            tool="t",
            args_digest="d",
        )


def test_commit_result_provenance_path_refuses_unrecorded() -> None:
    """_commit_result / Result provenance must not read AttemptUnrecorded as str."""
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:provenance"):
        require_attempt_str(dict(_UNREC), field="provenance")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prov",
    list(get_args(Provenance)),
    ids=list(get_args(Provenance)),
)
async def test_executor_record_accepts_every_provenance_literal(tmp_path, clock, prov) -> None:
    """Public record path accepts exactly the Provenance literals (_as_provenance)."""
    sid = f"st_prov_{prov}"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    try:
        _, arts = attach_executor(rt, tmp_path)
        aid = await rt.start_attempt("SPEC", provenance=prov)
        await record_success(rt, aid, arts)
        assert rt.results[result_id(aid)]["provenance"] == prov
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_executor_record_refuses_unknown_provenance(tmp_path, catalog, clock) -> None:
    """record (public path) raises plain ValueError on a recorded unknown value."""
    store = SqliteEventStore("st_prov_bad", tmp_path / "e.db", clock=clock)
    await store.acquire_lease(new_owner())
    writer = StudyWriter(store)
    writer.start()
    arts = LocalArtifactStore(tmp_path / "a")
    art = arts.put(
        b'{"count":225}',
        make_tool_ref("st_prov_bad/SPEC#1:1"),
        name="main",
        media_type="application/json",
    )
    task = Task(
        task_id="st_prov_bad/SPEC",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="SPEC",
            title="t",
            goal="g",
            route="experiment",
            criteria=[RequirementRef(template_id="value_match")],
        ),
        state=TaskState.RUNNING,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["st_prov_bad/SPEC#1"],
    )
    attempt: dict[str, object] = {
        "attempt_id": "st_prov_bad/SPEC#1",
        "closed": False,
        "provenance": "invented",
        "task_id": task.task_id,
        "study_id": "st_prov_bad",
    }

    def _get_attempt(_attempt_id: str) -> dict[str, object]:
        return attempt

    def _get_task(_task_id: str) -> Task:
        return task

    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=_get_attempt,
        get_task=_get_task,
        max_attempts=3,
    )
    try:
        with pytest.raises(ValueError, match=r"invalid attempt provenance: 'invented'"):
            await ex.record(
                "st_prov_bad/SPEC#1",
                AgentTaskOutput(
                    summary="ok",
                    outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                    values={"count": 225},
                ),
            )
    finally:
        await writer.stop()
        await store.close()


def test_attempt_row_is_open_refuses_non_mapping_row() -> None:
    from coscientist.core.events.attempt_fields import attempt_row_is_open

    with pytest.raises(TypeError, match=r"attempt row must be a mapping, got str"):
        attempt_row_is_open({"a1": "bad"}, "a1")
