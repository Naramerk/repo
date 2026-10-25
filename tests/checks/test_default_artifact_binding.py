"""Default artifact_present binds to the task's required expected outputs (PI1b)."""

from __future__ import annotations

from pathlib import Path

from coscientist.checks.evaluate import evaluate_task_checks
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    OutputSpec,
    ReqStatus,
    TaskSpec,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _store_with_artifact(tmp_path: Path, name: str, arts: LocalArtifactStore) -> ArtifactHandle:
    body = b'{"ok":true}'
    art = arts.put(body, make_tool_ref(f"call:{name}"), name=name, media_type="application/json")
    return ArtifactHandle(artifact_id=art.artifact_id, name=name)


def test_default_binds_to_declared_outputs_any_name(tmp_path: Path) -> None:
    """Tool-id-named outputs (intent-derived plans) satisfy the default check."""
    catalog = minimal_test_catalog()  # artifact_present pinned to output "main"
    arts = LocalArtifactStore(tmp_path / "arts")
    handle = _store_with_artifact(tmp_path, "static:heracleu:overview", arts)
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        expected_outputs=[
            OutputSpec(name="static:heracleu:overview", media_type="application/json", required=True)
        ],
    )
    out = AgentTaskOutput(summary="x", outputs={"static:heracleu:overview": handle})
    (result,) = [r for r in evaluate_task_checks(catalog, spec, out, arts) if r.template_id == "artifact_present"]
    assert result.status is ReqStatus.MET
    assert result.evidence and result.evidence[0].artifact_id == handle.artifact_id


def test_default_fails_when_declared_output_missing(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        expected_outputs=[
            OutputSpec(name="static:heracleu:overview", media_type="application/json", required=True)
        ],
    )
    out = AgentTaskOutput(summary="x", outputs={})  # tool never delivered
    (result,) = [r for r in evaluate_task_checks(catalog, spec, out, arts) if r.template_id == "artifact_present"]
    assert result.status is ReqStatus.FAILED
    assert result.detail == "output_not_submitted:static:heracleu:overview"


def test_default_fails_when_declared_artifact_not_in_store(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    ghost = ArtifactHandle(artifact_id="art_missing", name="static:x")
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        expected_outputs=[OutputSpec(name="static:x", media_type="application/json", required=True)],
    )
    out = AgentTaskOutput(summary="x", outputs={"static:x": ghost})
    (result,) = [r for r in evaluate_task_checks(catalog, spec, out, arts) if r.template_id == "artifact_present"]
    assert result.status is ReqStatus.FAILED
    assert result.detail == "artifact_missing:static:x"


def test_no_declared_outputs_keeps_legacy_template_literal_lookup(tmp_path: Path) -> None:
    """Base semantics: undeclared-but-submitted ``main`` still METs (foreach quorum
    bodies); nothing submitted stays FAILED output_not_submitted — never a silent
    pass, never a raise (ResultRecorded must happen)."""
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    spec = TaskSpec(task_key="B", title="B", goal="g", route="experiment")
    handle = _store_with_artifact(tmp_path, "main", arts)
    ok = AgentTaskOutput(summary="x", outputs={"main": handle})
    (result,) = evaluate_task_checks(catalog, spec, ok, arts)
    assert result.status is ReqStatus.MET
    empty = AgentTaskOutput(summary="x", outputs={})
    (result2,) = evaluate_task_checks(catalog, spec, empty, arts)
    assert result2.status is ReqStatus.FAILED
    assert result2.detail == "output_not_submitted"


def test_plan_authored_criteria_keep_template_pinned_output(tmp_path: Path) -> None:
    """Explicit criteria do NOT rebind: prereg single-source stays on the template."""
    from coscientist.core.execution.domain import RequirementRef

    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="artifact_present")],
        expected_outputs=[OutputSpec(name="static:x", media_type="application/json", required=True)],
    )
    handle = _store_with_artifact(tmp_path, "static:x", arts)
    out = AgentTaskOutput(summary="x", outputs={"static:x": handle})
    (result,) = evaluate_task_checks(catalog, spec, out, arts)
    assert result.template_id == "artifact_present"
    assert result.status is ReqStatus.FAILED  # template literal "main" not submitted
