"""AttemptUnrecorded: consumers match + typed refuse (NeedUnrecorded-style)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.events.attempt_fields import (
    AttemptUnrecordedRefuse,
    attempt_closed,
    project_attempt_started_payload,
    require_attempt_bool,
    require_attempt_str,
)
from coscientist.core.events.events import StoredEvent, make_idem
from coscientist.core.events.payload_schemas import AttemptUnrecorded
from coscientist.core.events.upcasters import ATTEMPT_STARTED_UNRECORDED_SINCE
from coscientist.core.execution.domain import AgentTaskOutput
from coscientist.tools.effects import EffectIndex
from tests.helpers import close_runtime, open_runtime

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
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:closed"):
        await rt.executor.mark_route_returned("att_x", AgentTaskOutput(claim=None))
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
    with pytest.raises(AttemptUnrecordedRefuse, match=r"attempt_field_unrecorded:study_id"):
        await rt.executor.record("att_y", AgentTaskOutput(claim=None))
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


def test_attempt_row_is_open_refuses_non_mapping_row() -> None:
    from coscientist.core.events.attempt_fields import attempt_row_is_open

    with pytest.raises(TypeError, match=r"attempt row must be a mapping, got str"):
        attempt_row_is_open({"a1": "bad"}, "a1")
