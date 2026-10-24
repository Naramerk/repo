"""Fail-closed EventType payload schemas on replay."""

from __future__ import annotations

from typing import Any, cast, get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.errors import JournalFormatError, JournalPayloadError
from coscientist.core.events.events import EventType
from coscientist.core.events.payload_schemas import (
    PAYLOAD_MODELS,
    AttemptStartedPayload,
    ExcludedClaimRow,
    WireModel,
    _apply_writer_omitted,
    validate_event_payload,
)
from coscientist.core.events.schema_gate import _payload_validators, upcast_strict
from coscientist.core.execution.domain import HitlPolicy, Study


def _study_created_payload(**over: object) -> dict:
    """Writer-shaped StudyCreated wire dump (ISO datetimes)."""
    from datetime import UTC, datetime

    base = Study(
        study_id="s",
        profile="research",
        request="q",
        catalog_version=0,
        hitl_policy=HitlPolicy(),
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        require_hypotheses=False,
    ).model_dump(mode="json")
    base.update(over)
    return base


def test_payload_validators_cover_every_event_type() -> None:
    reg = _payload_validators()
    assert set(reg) == set(get_args(EventType))
    assert set(PAYLOAD_MODELS) <= set(get_args(EventType))
    assert set(PAYLOAD_MODELS).issubset(set(reg))


def test_payload_validator_missing_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    import coscientist.core.events.schema_gate as sg

    monkeypatch.setattr(sg, "_payload_validators", lambda: {})
    with pytest.raises(JournalFormatError, match="no payload validator registered"):
        upcast_strict("StudyCreated", 1, _study_created_payload())


def test_upcast_strict_validates_study_created() -> None:
    version, out = upcast_strict("StudyCreated", 13, _study_created_payload())
    assert version >= 1
    assert out["study_id"] == "s"
    assert out["catalog_version"] == 0


def test_upcast_strict_refuses_sparse_study_created() -> None:
    # G4: v13→v14 upcaster refuses missing require_hypotheses before schema gate.
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="require_hypotheses_required"):
        upcast_strict("StudyCreated", 13, {"study_id": "s", "catalog_version": 0})
    # Writer-version sparse dump (already v14) → payload schema refuse.
    with pytest.raises(JournalPayloadError, match="payload schema refuse") as ei:
        validate_event_payload(
            "StudyCreated", {"study_id": "s", "catalog_version": 0}
        )
    assert ei.value.error_type == "missing"


def test_upcast_strict_refuses_missing_required_field() -> None:
    with pytest.raises(JournalPayloadError, match="payload schema refuse") as ei:
        upcast_strict("BudgetExceeded", 1, {})
    assert ei.value.error_type == "missing"


def test_validate_event_payload_type_error() -> None:
    with pytest.raises(JournalPayloadError, match="payload schema refuse") as ei:
        validate_event_payload("BudgetExceeded", {"scope": 1})
    assert ei.value.error_type == "int_type" or ei.value.error_type == "string_type"


def test_validate_event_payload_returns_raw_unchanged() -> None:
    raw = {"scope": "study"}
    out = validate_event_payload("BudgetExceeded", raw)
    assert out is raw


def test_validate_event_payload_forbids_extra_keys() -> None:
    with pytest.raises(JournalPayloadError, match="payload schema refuse") as ei:
        validate_event_payload("BudgetExceeded", {"scope": "study", "extra_ignored": True})
    assert ei.value.error_type == "extra_forbidden"


def test_validate_event_payload_untyped_event_refuses() -> None:
    with pytest.raises(JournalPayloadError, match="no payload schema registered") as ei:
        validate_event_payload("NotAnEventType", {"x": 1})
    assert ei.value.error_type == "untyped_payload"


def test_validate_event_payload_non_dict_refuses() -> None:
    bad: object = ["not", "a", "dict"]
    with pytest.raises(JournalPayloadError, match="payload must be object") as ei:
        validate_event_payload("BudgetExceeded", cast(dict[str, Any], bad))
    assert ei.value.error_type == "dict_type"


def test_apply_writer_omitted_fills_only_listed_keys() -> None:
    out = _apply_writer_omitted({"a": 1}, frozenset({"b", "c"}))
    assert out == {"a": 1, "b": None, "c": None}


def test_apply_writer_omitted_noop_when_complete() -> None:
    raw = {"a": 1, "b": 2}
    assert _apply_writer_omitted(raw, frozenset({"b"})) is raw


def test_apply_writer_omitted_noop_on_non_dict() -> None:
    assert _apply_writer_omitted([1], frozenset({"a"})) == [1]


def test_wire_model_writer_omitted_absent_key_validates() -> None:
    class Row(WireModel):
        _writer_omitted = frozenset({"opt"})
        need: str
        opt: str | None

    Row.model_validate({"need": "x"})


def test_wire_model_refuses_absent_key_outside_omitted() -> None:
    class Row(WireModel):
        _writer_omitted = frozenset()
        need: str
        opt: str | None

    with pytest.raises(ValidationError, match="opt"):
        Row.model_validate({"need": "x"})


def test_source_fetched_omits_attempt_id() -> None:
    raw = {"call_id": "c", "digest": "d", "tool": "t", "url": "u"}
    out = validate_event_payload("SourceFetched", raw)
    assert out is raw
    assert "attempt_id" not in out


def test_task_created_omits_expand_keys() -> None:
    from coscientist.core.execution.domain import OutputSpec, TaskSpec

    raw = {
        "task_id": "st/SPEC",
        "kind": "spec",
        "spec": TaskSpec(
            task_key="SPEC",
            title="t",
            goal="g",
            route="r",
            expected_outputs=[OutputSpec(name="o", media_type="text/plain")],
        ).model_dump(mode="json"),
        "state": "PENDING",
        "outcome": None,
        "reason": None,
        "deadline": "2026-01-01T00:00:00+00:00",
        "attempts": [],
        "inherited_from": None,
    }
    out = validate_event_payload("TaskCreated", raw)
    assert out is raw


def test_attempt_started_legacy_without_data_refuses_at_schema() -> None:
    """Schema must not invent; sparse raw (pre-upcast) refuses."""
    raw = {
        "attempt_id": "st/SPEC#1",
        "task_id": "st/SPEC",
        "task_key": "legacy",
        "deadline": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(ValidationError):
        AttemptStartedPayload.model_validate(raw)


def test_attempt_started_v11_unrecorded_upcast() -> None:
    """v10→v11: absent forever-fields → AttemptUnrecorded; task_key dropped.

    Provenance is required at v10 (R16a) — supply it; other forever-fields Unrecorded.
    """
    from coscientist.core.events.schema_gate import upcast_strict
    from coscientist.core.events.upcasters import ATTEMPT_STARTED_UNRECORDED_SINCE

    version, out = upcast_strict(
        "AttemptStarted",
        1,
        {
            "attempt_id": "att_lit",
            "task_id": "st_reclass_old/E1",
            "task_key": "E1",
            "inputs": [],
            "tainted_context": False,
            "provenance": "real",
        },
    )
    assert version == 11
    assert "task_key" not in out
    assert out["provenance"] == "real"
    for field in (
        "n",
        "dispatch_id",
        "route_returned",
        "closed",
        "study_id",
    ):
        assert out[field] == {
            "kind": "unrecorded",
            "source_schema_version": ATTEMPT_STARTED_UNRECORDED_SINCE,
        }
    assert out["attempt_role"] == "agent"
    assert out["limitations"] == []


def test_graph_attrs_registry_attr_type() -> None:
    from coscientist.core.events.payload_schemas import GraphNodeUpsertedPayload

    GraphNodeUpsertedPayload.model_validate(
        {
            "node_id": "n1",
            "kind": "claim",
            "study_id": "s",
            "attrs": {
                "target": "H1",
                "checked_against": "CR1",
                "stance": "supports",
            },
            "ref_id": None,
            "parent_id": None,
            "created_at": None,
        }
    )
    # KindSpec field with wrong AttrType → refuse
    with pytest.raises(ValidationError, match=r"stance: expected type"):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "claim",
                "study_id": "s",
                "attrs": {
                    "target": "H1",
                    "checked_against": "CR1",
                    "stance": 1,
                },
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )
    # Unknown kind → refuse
    with pytest.raises(ValidationError, match=r"unknown node kind"):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "not_a_kind",
                "study_id": "s",
                "attrs": {},
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_graph_attrs_unknown_key_refused() -> None:
    from coscientist.core.events.payload_schemas import GraphNodeUpsertedPayload

    with pytest.raises(ValidationError, match=r"claim unknown attrs: \['nope'\]"):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "claim",
                "study_id": "s",
                "attrs": {
                    "target": "H1",
                    "checked_against": "CR1",
                    "stance": "supports",
                    "nope": True,
                },
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_graph_attrs_missing_required_refused() -> None:
    from coscientist.core.events.payload_schemas import GraphNodeUpsertedPayload

    with pytest.raises(
        ValidationError,
        match=r"claim missing required attrs: \['checked_against', 'target'\]",
    ):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "claim",
                "study_id": "s",
                "attrs": {"stance": "supports"},
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_excluded_claim_row_omits_optional_ids() -> None:
    ExcludedClaimRow.model_validate({"reason": "invalid"})
