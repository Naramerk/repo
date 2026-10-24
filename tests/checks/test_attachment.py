"""Tests for coscientist.checks.attachment (CAPABILITIES #7 first slice)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from coscientist.checks.attachment import (
    CheckLike,
    code_checks,
    refuse_unassessed_failed_coercion,
    require_code_and_review,
    review_checks,
    unassessed_required_review_ids,
)
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import ReqStatus


@dataclass(frozen=True)
class _Row:
    """Minimal CheckLike double (no CheckResult defaults / heuristics)."""

    check_id: str
    status: ReqStatus
    is_code: bool
    required: bool


def _code(cid: str, status: ReqStatus = ReqStatus.MET, *, required: bool = True) -> CheckLike:
    return _Row(check_id=cid, status=status, is_code=True, required=required)


def _review(
    cid: str, status: ReqStatus = ReqStatus.UNASSESSED, *, required: bool = True
) -> CheckLike:
    return _Row(check_id=cid, status=status, is_code=False, required=required)


def test_code_checks_filters_is_code_true():
    rows = [_code("c1"), _review("r1"), _code("c2", ReqStatus.FAILED)]
    assert [c.check_id for c in code_checks(rows)] == ["c1", "c2"]


def test_review_checks_filters_is_code_false():
    rows = [_code("c1"), _review("r1"), _review("r2", ReqStatus.MET)]
    assert [c.check_id for c in review_checks(rows)] == ["r1", "r2"]


def test_require_code_and_review_ok_returns_both_sides():
    code, review = require_code_and_review([_code("c1"), _review("r1")])
    assert [c.check_id for c in code] == ["c1"]
    assert [c.check_id for c in review] == ["r1"]


def test_require_code_and_review_missing_code_refuses():
    with pytest.raises(CatalogError, match="missing CodeCheck"):
        require_code_and_review([_review("r1")])


def test_require_code_and_review_missing_review_refuses():
    with pytest.raises(CatalogError, match="missing ReviewCheck"):
        require_code_and_review([_code("c1")])


def test_require_code_and_review_empty_refuses_code_first():
    with pytest.raises(CatalogError, match="missing CodeCheck"):
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


def test_unassessed_required_review_ids_ignores_code_rows():
    rows = [_code("c_u", ReqStatus.UNASSESSED), _review("r1", ReqStatus.FAILED)]
    assert unassessed_required_review_ids(rows) == []


def test_refuse_unassessed_to_failed():
    with pytest.raises(CatalogError, match="UNASSESSED must stay distinct"):
        refuse_unassessed_failed_coercion(ReqStatus.UNASSESSED, ReqStatus.FAILED)


def test_refuse_failed_to_unassessed():
    with pytest.raises(CatalogError, match="UNASSESSED must stay distinct"):
        refuse_unassessed_failed_coercion(ReqStatus.FAILED, ReqStatus.UNASSESSED)


def test_refuse_coercion_same_status_noop():
    refuse_unassessed_failed_coercion(ReqStatus.UNASSESSED, ReqStatus.UNASSESSED)
    refuse_unassessed_failed_coercion(ReqStatus.FAILED, ReqStatus.FAILED)


def test_refuse_coercion_other_pairs_allowed():
    refuse_unassessed_failed_coercion(ReqStatus.PENDING, ReqStatus.UNASSESSED)
    refuse_unassessed_failed_coercion(ReqStatus.UNASSESSED, ReqStatus.MET)
    refuse_unassessed_failed_coercion(ReqStatus.FAILED, ReqStatus.MET)
