"""Fail-closed EventType payload schemas on replay."""

from __future__ import annotations

from typing import get_args

import pytest

from coscientist.core.events.errors import JournalFormatError, JournalPayloadError
from coscientist.core.events.events import EventType
from coscientist.core.events.payload_schemas import PAYLOAD_MODELS, validate_event_payload
from coscientist.core.events.schema_gate import _payload_validators, upcast_strict


def test_payload_validators_cover_every_event_type() -> None:
    reg = _payload_validators()
    assert set(reg) == set(get_args(EventType))
    assert set(PAYLOAD_MODELS) <= set(get_args(EventType))
    assert set(PAYLOAD_MODELS).issubset(set(reg))


def test_payload_validator_missing_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    import coscientist.core.events.schema_gate as sg

    monkeypatch.setattr(sg, "_payload_validators", lambda: {})
    with pytest.raises(JournalFormatError, match="no payload validator registered"):
        upcast_strict("StudyCreated", 1, {"study_id": "s", "catalog_version": 0})


def test_upcast_strict_validates_study_created() -> None:
    version, out = upcast_strict("StudyCreated", 1, {"study_id": "s", "catalog_version": 0})
    assert version >= 1
    assert out["study_id"] == "s"
    assert out["catalog_version"] == 0


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
