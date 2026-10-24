"""Tests for coscientist.checks.attachment (CAPABILITIES #7 / Q0d)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from coscientist.checks.attachment import (
    UNASSESSED_PARTNERS,
    CheckLike,
    code_checks,
    refuse_illegal_unassessed_transition,
    require_code_and_review,
    review_checks,
    unassessed_required_review_ids,
)
from coscientist.checks.evaluate import (
    agent_facing_template_view,
    template_to_requirement,
)
from coscientist.core.events.errors import CheckAttachmentError
from coscientist.core.execution.domain import ReqStatus, RequirementTemplate, ReviewCheck

_DISTINCT = frozenset({ReqStatus.FAILED, ReqStatus.STALE, ReqStatus.MIGRATION_ERROR})


@dataclass(frozen=True)
class _Row:
    check_id: str
    status: ReqStatus
    is_code: bool
    required: bool


def _code(cid: str, status: ReqStatus = ReqStatus.MET, *, required: bool = True) -> CheckLike:
    return _Row(check_id=cid, status=status, is_code=True, required=required)


def _review(cid: str, status: ReqStatus = ReqStatus.UNASSESSED, *, required: bool = True) -> CheckLike:
    return _Row(check_id=cid, status=status, is_code=False, required=required)


def _review_tmpl() -> RequirementTemplate:
    return RequirementTemplate(
        id="rev",
        title="rev",
        text_ru="Обзор",
        version=1,
        required=True,
        role="validity",
        method_sources=["t"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        exempt_from_freeze=True,
        check=ReviewCheck(reviewer_role="critic", question="ok?"),
    )


def test_code_checks_filters_code_kind():
    rows = [_code("c1"), _review("r1"), _code("c2", ReqStatus.FAILED)]
    assert [c.check_id for c in code_checks(rows)] == ["c1", "c2"]


def test_review_checks_filters_review_kind_not_complement():
    rows = [_code("c1"), _review("r1"), _review("r2", ReqStatus.MET)]
    got = review_checks(rows)
    assert [c.check_id for c in got] == ["r1", "r2"]
    assert [c.is_code for c in got] == [False, False]


def test_require_code_and_review_ok():
    code, review = require_code_and_review([_code("c1"), _review("r1")])
    assert [c.check_id for c in code] == ["c1"]
    assert [c.check_id for c in review] == ["r1"]


def test_require_code_and_review_missing_code():
    with pytest.raises(CheckAttachmentError, match="missing CodeCheck"):
        require_code_and_review([_review("r1")])


def test_require_code_and_review_missing_review():
    with pytest.raises(CheckAttachmentError, match="missing ReviewCheck"):
        require_code_and_review([_code("c1")])


def test_require_code_and_review_empty_missing_code_first():
    with pytest.raises(CheckAttachmentError, match="missing CodeCheck"):
        require_code_and_review([])


def test_unassessed_required_review_ids_only_unassessed_or_pending():
    rows = [
        _code("c1"),
        _review("r_u", ReqStatus.UNASSESSED),
        _review("r_p", ReqStatus.PENDING),
        _review("r_f", ReqStatus.FAILED),
        _review("r_m", ReqStatus.MET),
        _review("r_opt", ReqStatus.UNASSESSED, required=False),
    ]
    assert unassessed_required_review_ids(rows) == ["r_u", "r_p"]


def test_unassessed_required_review_ids_excludes_stale_and_migration_error():
    rows = [
        _review("r_s", ReqStatus.STALE),
        _review("r_e", ReqStatus.MIGRATION_ERROR),
        _review("r_u", ReqStatus.UNASSESSED),
    ]
    assert unassessed_required_review_ids(rows) == ["r_u"]


def test_unassessed_required_review_ids_ignores_code_rows():
    rows = [_code("c_u", ReqStatus.UNASSESSED), _review("r1", ReqStatus.FAILED)]
    assert unassessed_required_review_ids(rows) == []


@pytest.mark.parametrize("other", list(UNASSESSED_PARTNERS))
def test_refuse_allows_unassessed_to_partner(other: ReqStatus) -> None:
    refuse_illegal_unassessed_transition(ReqStatus.UNASSESSED, other)


@pytest.mark.parametrize("other", list(UNASSESSED_PARTNERS))
def test_refuse_allows_partner_to_unassessed(other: ReqStatus) -> None:
    refuse_illegal_unassessed_transition(other, ReqStatus.UNASSESSED)


@pytest.mark.parametrize("other", list(_DISTINCT))
def test_refuse_blocks_unassessed_to_distinct(other: ReqStatus) -> None:
    with pytest.raises(CheckAttachmentError, match="UNASSESSED must stay distinct"):
        refuse_illegal_unassessed_transition(ReqStatus.UNASSESSED, other)


@pytest.mark.parametrize("other", list(_DISTINCT))
def test_refuse_blocks_distinct_to_unassessed(other: ReqStatus) -> None:
    with pytest.raises(CheckAttachmentError, match="UNASSESSED must stay distinct"):
        refuse_illegal_unassessed_transition(other, ReqStatus.UNASSESSED)


def test_refuse_unassessed_same_status_noop() -> None:
    refuse_illegal_unassessed_transition(ReqStatus.UNASSESSED, ReqStatus.UNASSESSED)


def test_refuse_pending_to_failed_out_of_scope() -> None:
    refuse_illegal_unassessed_transition(ReqStatus.PENDING, ReqStatus.FAILED)


def test_refuse_pending_to_stale_out_of_scope() -> None:
    refuse_illegal_unassessed_transition(ReqStatus.PENDING, ReqStatus.STALE)


def test_requirement_template_check_object_validation_error():
    """Raw model_validate payload with non-Code/Review check → ValidationError."""
    with pytest.raises(ValidationError, match="instance of CodeCheck"):
        RequirementTemplate.model_validate(
            {
                "id": "weird",
                "title": "w",
                "text_ru": "t",
                "version": 1,
                "required": True,
                "role": "prediction",
                "method_sources": ["t"],
                "rule_ids": ["R23"],
                "applies_to": ["experiment"],
                "exempt_from_freeze": True,
                "check": "not-a-code-or-review-check",
            }
        )


def test_agent_facing_review_check_detail():
    view = agent_facing_template_view(_review_tmpl())
    assert view["is_code"] is False
    assert view["check_detail"] == "critic"
    assert view["version"] == 1
    assert "check_kind" not in view


def test_template_to_requirement_review_is_not_code():
    req = template_to_requirement(_review_tmpl(), "n1", req_id="n1:rev")
    assert req.is_code is False
    assert req.check_version == 1
