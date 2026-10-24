"""Typed harness errors. Callers must handle these; nothing is swallowed."""

from __future__ import annotations


class BenchError(Exception):
    """Base class for benchmark harness failures."""


class BenchConfigError(BenchError):
    """Task set, runner, or harness configuration is missing or invalid."""


class BenchTokenError(BenchError):
    """Provider token usage was missing, incomplete, or internally inconsistent.

    The harness never estimates tokens. A run that cannot supply provider usage
    must return ``RunnerRefuse`` with reason_code ``tokens_unavailable``.
    """


class BenchAdapterError(BenchError):
    """An old/new system adapter refused or could not be constructed."""
