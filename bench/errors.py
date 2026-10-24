"""Typed harness errors. Callers must handle these; nothing is swallowed."""

from __future__ import annotations


class BenchError(Exception):
    """Base class for benchmark harness failures."""


class BenchConfigError(BenchError):
    """Task set, manifest, runner, or harness configuration is missing or invalid."""
