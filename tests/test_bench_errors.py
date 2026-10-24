"""Hierarchy checks for bench.errors (plan item 2)."""

from __future__ import annotations

from bench.errors import BenchConfigError, BenchError


def test_bench_config_error_subclass_of_bench_error() -> None:
    assert issubclass(BenchConfigError, BenchError)


def test_bench_error_subclass_of_exception() -> None:
    assert issubclass(BenchError, Exception)
