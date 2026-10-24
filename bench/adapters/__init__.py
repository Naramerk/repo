"""Adapters that implement ``BenchRunner`` for old and new CoScientist."""

from __future__ import annotations

from bench.adapters.new_runner import NewCoScientistRunner, NewRunnerConfig
from bench.adapters.old_runner import OldCoScientistRunner, OldRunnerConfig

__all__ = [
    "NewCoScientistRunner",
    "NewRunnerConfig",
    "OldCoScientistRunner",
    "OldRunnerConfig",
]
