"""Unit tests for bench.errors (plan item 2)."""

from __future__ import annotations

import pytest

from bench.errors import BenchConfigError, BenchError


def test_bench_config_error_message() -> None:
    err = BenchConfigError("missing manifest")
    assert type(err) is BenchConfigError
    assert str(err) == "missing manifest"


def test_bench_error_raise_and_catch() -> None:
    with pytest.raises(BenchError, match="refused"):
        raise BenchError("refused")


def test_bench_config_error_is_catchable_as_bench_error() -> None:
    with pytest.raises(BenchError, match="invalid task"):
        raise BenchConfigError("invalid task")
