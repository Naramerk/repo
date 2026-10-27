"""HYP v2 schema contract (docs/plan/hypothesis-v2.md §3.1).

Write path is fail-closed v2 (prediction/rank/question_ref/selected required,
>=3 rows, unique ids/ranks, exactly one selected); there is no v1 read path
(legacy v1 rows are rejected by the gates).
"""

import pytest
from pydantic import ValidationError

from coscientist.agents.schemas import HypothesisDraft, HypothesisOut


def _row(i: int, *, selected: bool | None = None) -> HypothesisDraft:
    return HypothesisDraft(
        id=f"h{i}",
        statement=f"s{i}",
        rationale=f"r{i}",
        prediction=f"p{i}",
        rank=i,
        question_ref="q1",
        selected=(i == 0) if selected is None else selected,
    )


def test_v2_out_accepts_competing_set() -> None:
    out = HypothesisOut(hypotheses=(_row(0), _row(1), _row(2)))
    assert [h.id for h in out.hypotheses] == ["h0", "h1", "h2"]
    assert sum(1 for h in out.hypotheses if h.selected) == 1


def test_v2_requires_at_least_three() -> None:
    with pytest.raises(ValidationError):
        HypothesisOut(hypotheses=(_row(0), _row(1)))


def test_v2_requires_exactly_one_selected() -> None:
    rows = [_row(i, selected=False) for i in range(3)]
    with pytest.raises(ValidationError, match="exactly one hypothesis must be selected"):
        HypothesisOut(hypotheses=tuple(rows))
    two = [_row(0, selected=True), _row(1, selected=True), _row(2, selected=False)]
    with pytest.raises(ValidationError, match="exactly one hypothesis must be selected"):
        HypothesisOut(hypotheses=tuple(two))


def _dup(kind: str, i: int) -> HypothesisDraft:
    """Third row cloning id (kind='id') or rank (kind='rank') of row 0/1."""
    base: dict[str, object] = {
        "id": f"x{i}",
        "statement": "x",
        "rationale": "y",
        "prediction": "p",
        "rank": 9,
        "question_ref": "q1",
        "selected": False,
    }
    if kind == "id":
        base["id"] = "h0"
    else:
        base["rank"] = 1
    return HypothesisDraft.model_validate(base)


def test_v2_requires_unique_ids_and_ranks() -> None:
    with pytest.raises(ValidationError, match="ids must be unique"):
        HypothesisOut(hypotheses=(_row(0), _row(1), _dup("id", 3)))
    with pytest.raises(ValidationError, match="ranks must be unique"):
        HypothesisOut(hypotheses=(_row(0), _row(1), _dup("rank", 3)))


@pytest.mark.parametrize("missing", ["prediction", "rank", "question_ref", "selected"])
def test_v2_rows_fail_closed_on_missing_v2_field(missing: str) -> None:
    base: dict[str, object] = {
        "id": "h1", "statement": "s", "rationale": "r",
        "prediction": "p", "rank": 0, "question_ref": "q1", "selected": True,
    }
    base.pop(missing)
    with pytest.raises(ValidationError):
        HypothesisDraft.model_validate(base)
