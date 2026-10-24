"""Unit tests for bench.errors (plan item 2)."""

from __future__ import annotations

import pytest

from bench.errors import BenchConfigError, BenchError


def test_bench_config_error_subclass_of_bench_error() -> None:
    assert issubclass(BenchConfigError, BenchError)


def test_bench_error_subclass_of_exception() -> None:
    assert issubclass(BenchError, Exception)


def test_bench_config_error_catchable_as_bench_error_with_message() -> None:
    """Subclass raises are catchable as BenchError; message matches for callers."""
    with pytest.raises(BenchError, match="missing manifest") as ei:
        raise BenchConfigError("missing manifest")
    assert isinstance(ei.value, BenchConfigError)


def test_bench_error_catchable_as_exception_with_message() -> None:
    """Base harness error is a normal Exception; message matches for callers."""
    with pytest.raises(Exception, match="refused") as ei:
        raise BenchError("refused")
    assert isinstance(ei.value, BenchError)
