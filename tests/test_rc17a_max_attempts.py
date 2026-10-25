"""RC17a (#99): Study.max_attempts required; profile→Study; typed refuse."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coscientist.api.studies import CreateStudyBody
from coscientist.core.events.errors import CatalogError
from coscientist.core.events.events import (
    STUDY_CREATED_MAX_ATTEMPTS_SINCE,
    STUDY_CREATED_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import HitlPolicy, Study
from coscientist.profiles.load import load_profile

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DL = datetime(2026, 1, 2, tzinfo=UTC)


def _study_kwargs(**over: object) -> dict:
    base: dict = {
        "study_id": "s",
        "profile": "research",
        "request": "q",
        "catalog_version": 3,
        "hitl_policy": HitlPolicy(),
        "deadline": _DL,
        "created_at": _NOW,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
        "max_attempts": 3,
        "require_hypotheses": False,
    }
    base.update(over)
    return base


def _v15_payload(**over: object) -> dict:
    base: dict = {
        "study_id": "s",
        "catalog_version": 3,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
    }
    base.update(over)
    return base


def test_missing_max_attempts_refuses() -> None:
    """Study / CreateStudyBody / upcast: missing max_attempts → typed refuse."""
    kw = _study_kwargs()
    del kw["max_attempts"]
    with pytest.raises(ValidationError, match="max_attempts"):
        Study(**kw)

    body: dict[str, object] = {
        "profile": "research",
        "request": "q",
        "catalog_version": 3,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
    }
    with pytest.raises(ValidationError, match="max_attempts"):
        CreateStudyBody.model_validate(body)

    payload = _v15_payload()
    with pytest.raises(CatalogError, match=r"StudyCreated\.v16:max_attempts_required"):
        upcast("StudyCreated", STUDY_CREATED_MAX_ATTEMPTS_SINCE, payload)


def test_study_created_v16_keeps_max_attempts() -> None:
    v, out = upcast(
        "StudyCreated",
        STUDY_CREATED_MAX_ATTEMPTS_SINCE,
        _v15_payload(max_attempts=7),
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert out["max_attempts"] == 7


def test_profile_research_max_attempts_wired() -> None:
    """profile.max_attempts is required and lands on packaged research.v1."""
    profile = load_profile("research", expected_tool_catalog_version=1)
    assert profile.max_attempts == 3


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (True, r"StudyCreated\.v16:max_attempts_must_be_int_got_bool"),
        (0, r"StudyCreated\.v16:max_attempts_must_be_ge_1"),
        ("3", r"StudyCreated\.v16:max_attempts_must_be_int_got_str"),
    ],
    ids=["bool", "zero", "str"],
)
def test_upcast_v16_max_attempts_typed_refuse(value: object, match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        upcast(
            "StudyCreated",
            STUDY_CREATED_MAX_ATTEMPTS_SINCE,
            _v15_payload(max_attempts=value),
        )


@pytest.mark.asyncio
async def test_create_study_syncs_executor_max_attempts(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """StudyCreated fold must set bound Executor.max_attempts (not leave invent-3)."""
    from datetime import timedelta

    from coscientist.core.events.clock import FakeClock
    from tests.helpers import close_runtime, open_runtime

    clock = FakeClock(start=_NOW)
    root = tmp_path_factory.mktemp("rc17a_exec_cap")
    rt = await open_runtime(root, "s-rc17a", clock=clock, owner="t1")
    try:
        assert rt.executor is not None
        # Empty journal open binds executor before StudyCreated → fail-closed 0.
        assert rt.executor.max_attempts == 0
        await rt.create_study(
            request="q",
            profile="research",
            catalog_version=1,
            hitl_policy=HitlPolicy(),
            deadline=_NOW + timedelta(hours=1),
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=7,
            require_hypotheses=False,
            inputs=[],
        )
        assert rt.max_attempts == 7
        assert rt.executor is not None
        assert rt.executor.max_attempts == 7
    finally:
        await close_runtime(rt)
