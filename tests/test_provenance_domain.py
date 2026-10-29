"""Direct tests for provenance helpers derived from ``Provenance``."""

from __future__ import annotations

import re

import pytest

from coscientist.core.execution.domain import (
    PROVENANCE_ORDER,
    PROVENANCE_RANK,
    VALID_PROVENANCE,
    Provenance,
    parse_provenance,
    worst_provenance,
)


def test_provenance_rank_order() -> None:
    assert PROVENANCE_ORDER == ("real", "partial", "stub")
    assert PROVENANCE_RANK == {"real": 0, "partial": 1, "stub": 2}
    assert VALID_PROVENANCE == frozenset({"real", "partial", "stub"})


@pytest.mark.parametrize("raw", ["real", "partial", "stub"], ids=["real", "partial", "stub"])
def test_parse_provenance_accepts_each_literal(raw: str) -> None:
    assert parse_provenance(raw, owner="o") == raw


def test_parse_provenance_requires_value() -> None:
    with pytest.raises(ValueError, match=re.escape("o: provenance required")):
        parse_provenance(None, owner="o")


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("invented", "'invented'"), ("", "''"), ("REAL", "'REAL'"), (1, "1"), (["real"], "['real']")],
    ids=["unknown-str", "empty-str", "wrong-case", "int", "list"],
)
def test_parse_provenance_rejects_unknown(raw: object, shown: str) -> None:
    with pytest.raises(ValueError, match=re.escape(f"o: unknown provenance {shown}")):
        parse_provenance(raw, owner="o")


@pytest.mark.parametrize(
    ("values", "expected"),
    [(("real", "partial"), "partial"), (("stub", "real"), "stub"), ((), "stub")],
    ids=["real-partial", "stub-real", "empty"],
)
def test_worst_provenance_uses_rank(values: tuple[Provenance, ...], expected: Provenance) -> None:
    assert worst_provenance(*values) == expected
