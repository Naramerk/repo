"""InputBinding modes use_partial|simulate|forbid."""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    InputSpec,
    TaskOutcome,
    TaskState,
)
from coscientist.core.execution.inputs import (
    InputUnavailable,
    bind_inputs,
    verdict_binding_digest,
    worst_provenance,
)
from coscientist.core.execution.machine import StudyRuntime, new_owner
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import make_task


def test_forbid_failed_dep_raises():
    dep = make_task("A", TaskOutcome.FAILED)
    child = make_task(
        "B",
        None,
        inputs=[InputSpec(from_task="A", output="main", mode="forbid")],
    )
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    with pytest.raises(InputUnavailable):
        bind_inputs(
            child,
            dep_tasks={"A": dep},
            dep_outputs={"A": {}},
            dep_provenance={"A": "real"},
        )


def test_simulate_when_missing():
    dep = make_task("A", TaskOutcome.FAILED)
    child = make_task(
        "B",
        None,
        inputs=[InputSpec(from_task="A", output="main", mode="simulate")],
    )
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    bindings, prov = bind_inputs(
        child,
        dep_tasks={"A": dep},
        dep_outputs={"A": {}},
        dep_provenance={"A": "real"},
    )
    assert bindings[0].source == "stub"
    assert prov == "stub"


def test_worst_provenance_order():
    assert worst_provenance("real", "partial") == "partial"
    assert worst_provenance("partial", "stub") == "stub"


_VERDICT_THRESHOLDS: dict[str, object] = {
    "refutations_needed": 1,
    "refutation_priority": False,
}
_VERDICT_PROVENANCE_CASES: list[tuple[dict[str, object], str]] = [
    ({"verdict": "supported", **_VERDICT_THRESHOLDS}, "provenance required"),
    (
        {"verdict": "supported", "provenance": None, **_VERDICT_THRESHOLDS},
        "provenance required",
    ),
    (
        {"verdict": "supported", "provenance": "bogus", **_VERDICT_THRESHOLDS},
        "unknown provenance",
    ),
]
_VERDICT_PROVENANCE_IDS = ["absent", "null", "unknown"]


@pytest.mark.parametrize(
    ("snap", "match"),
    _VERDICT_PROVENANCE_CASES,
    ids=_VERDICT_PROVENANCE_IDS,
)
def test_verdict_binding_digest_provenance_fail_closed(
    snap: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        verdict_binding_digest(ref="Q1", snapshot=snap)


@pytest.mark.parametrize(
    ("snap", "match"),
    _VERDICT_PROVENANCE_CASES,
    ids=_VERDICT_PROVENANCE_IDS,
)
def test_bind_verdict_provenance_fail_closed(
    snap: dict[str, object], match: str
) -> None:
    child = make_task(
        "B",
        None,
        inputs=[InputSpec(kind="verdict", ref="Q1")],
    )
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    with pytest.raises(InputUnavailable, match=match):
        bind_inputs(
            child,
            dep_tasks={},
            dep_outputs={},
            dep_provenance={},
            verdict_snapshots={"Q1": snap},
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "attempt_id": "a0",
                "task_id": "t0",
                "study_id": "st_prov",
            },
            "provenance required",
        ),
        (
            {
                "attempt_id": "a0",
                "task_id": "t0",
                "study_id": "st_prov",
                "provenance": None,
            },
            "provenance required",
        ),
        (
            {
                "attempt_id": "a0",
                "task_id": "t0",
                "study_id": "st_prov",
                "provenance": "bogus",
            },
            "unknown provenance",
        ),
    ],
    ids=["absent", "null", "unknown"],
)
@pytest.mark.asyncio
async def test_make_executor_default_attempt_provenance_fail_closed(
    tmp_path: Path,
    clock: FakeClock,
    payload: dict[str, object],
    match: str,
) -> None:
    sid = "st_prov"
    store = SqliteEventStore(sid, tmp_path / "e.db", clock=clock)
    await store.acquire_lease(new_owner())
    writer = StudyWriter(store)
    writer.start()
    catalog = minimal_test_catalog()
    rt = StudyRuntime(store, writer, clock=clock, owner=new_owner(), catalog=catalog)
    rt.attempts["a0"] = payload
    ex = rt.make_executor(
        catalog,
        LocalArtifactStore(tmp_path / "arts"),
    )
    with pytest.raises(ValueError, match=match):
        ex.get_attempt("a0")
