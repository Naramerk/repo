"""LocalProjectMixin fold contract: host attributes exist before folding.

Fold methods in ``coscientist.core.execution.local_project`` read their state
attributes directly; ``StudyRuntime.__init__`` is the single place that must
assign every name declared on the mixin before any event fold runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.local_project import LocalProjectMixin
from coscientist.core.execution.machine import StudyRuntime, new_owner
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _fresh_runtime(tmp_path: Path, clock: FakeClock) -> StudyRuntime:
    store = SqliteEventStore("st_mixin_contract", tmp_path / "e.db", clock=clock)
    return StudyRuntime(
        store,
        StudyWriter(store),
        clock=clock,
        owner=new_owner(),
        catalog=minimal_test_catalog(),
    )


def test_study_runtime_initializes_every_mixin_attribute(tmp_path, clock) -> None:
    """Direct reads in fold methods require __init__ to assign each mixin attr."""
    rt = _fresh_runtime(tmp_path, clock)
    missing = [name for name in LocalProjectMixin.__annotations__ if name not in vars(rt)]
    assert missing == []


def _flush(host: LocalProjectMixin) -> None:
    host._flush_pending_kind_stamps()


def _clear(host: LocalProjectMixin) -> None:
    host._clear_expand_request()


def _stamp(host: LocalProjectMixin) -> None:
    host._apply_kind_stamp_soft("art_1", "experiment_design")


@pytest.mark.parametrize(
    ("fold_call", "attr"),
    [
        pytest.param(_flush, "_pending_kind_stamps", id="flush-pending-kind-stamps"),
        pytest.param(_clear, "_expand_requests", id="clear-expand-request"),
        pytest.param(_stamp, "artifacts", id="apply-kind-stamp-soft"),
    ],
)
def test_fold_without_initialized_host_fails_fast(
    fold_call: Callable[[LocalProjectMixin], None], attr: str
) -> None:
    """A host that skips attribute initialization is a bug: the fold fails loudly."""
    with pytest.raises(
        AttributeError,
        match=re.escape(f"'LocalProjectMixin' object has no attribute '{attr}'"),
    ):
        fold_call(LocalProjectMixin())
