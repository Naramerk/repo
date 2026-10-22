"""Shared test helpers for M0."""

from __future__ import annotations

import json
from pathlib import Path

from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.executor import Executor
from coscientist.core.machine import StudyRuntime


def attach_executor(rt: StudyRuntime, tmp: Path) -> tuple[Executor, LocalArtifactStore]:
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp / "arts")
    ex = Executor(
        rt.writer,
        catalog,
        arts,
        clock=rt.clock,
        get_attempt=lambda aid: type("A", (), rt.attempts[aid])(),
        get_task=lambda tid: rt.tasks[tid],
        on_events=rt._project_local,
    )
    # patch closed/provenance attribute access for dict-backed attempts
    def get_attempt(aid: str):
        d = rt.attempts[aid]
        return type(
            "Att",
            (),
            {
                "attempt_id": d["attempt_id"],
                "closed": d.get("closed", False),
                "provenance": d.get("provenance", "real"),
                "task_id": d["task_id"],
                "study_id": d["study_id"],
            },
        )()

    ex.get_attempt = get_attempt
    rt.executor = ex
    return ex, arts


async def record_success(
    rt: StudyRuntime, aid: str, arts: LocalArtifactStore, *, count: int = 225
) -> None:
    assert rt.executor is not None
    content = json.dumps({"count": count}).encode()
    art = arts.put(
        content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
    )
    # ensure task criteria include value_match so SUCCESS is possible
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    if not task.spec.criteria:
        new_spec = task.spec.model_copy(
            update={"criteria": [RequirementRef(template_id="value_match")]}
        )
        rt.tasks[task.task_id] = task.model_copy(update={"spec": new_spec})
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": count},
        ),
    )


def exp_spec(key: str, deps: list[str] | None = None) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=key,
        goal="g",
        route="experiment",
        depends_on=deps or ["PLAN"],
        criteria=[RequirementRef(template_id="value_match")],
    )
