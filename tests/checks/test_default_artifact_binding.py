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


def _attempt_artifact(
    arts: LocalArtifactStore,
    attempt_id: str,
    name: str,
    *,
    provenance: str | None,
) -> ArtifactHandle:
    """Attempt-bound artifact with an explicit journal provenance stamp."""
    art = arts.put(
        b'{"ok":true}',
        make_tool_ref(f"{attempt_id}:{name}", tool=name),
        name=name,
        media_type="application/json",
        provenance=provenance,  # type: ignore[arg-type]
    )
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


def test_default_tool_named_output_with_tool_provenance_passes(tmp_path: Path) -> None:
    """tools_hint-named output backed by a recorded tool_call artifact → MET."""
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    handle = _attempt_artifact(arts, "a1", "static:calc", provenance="tool")
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        tools_hint=["static:calc"],
        expected_outputs=[OutputSpec(name="static:calc", media_type="application/json", required=True)],
    )
    out = AgentTaskOutput(summary="x", outputs={"static:calc": handle})
    (result,) = [
        r
        for r in evaluate_task_checks(catalog, spec, out, arts, attempt_id="a1")
        if r.template_id == "artifact_present"
    ]
    assert result.status is ReqStatus.MET


def test_default_values_echo_under_tool_name_fails_typed(tmp_path: Path) -> None:
    """Model-authored values echo under a tool-id name → typed provenance_violation.

    The journal stamps values echoes ``provenance="agent"``; a tool-id-named
    required output must instead carry the attempt's recorded tool_call bytes
    (``provenance="tool"``). Unstamped legacy artifacts fail the same way
    (fail-closed — no weakening of the existence gate).
    """
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    echo = _attempt_artifact(arts, "a1", "static:calc", provenance="agent")
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        tools_hint=["static:calc"],
        expected_outputs=[OutputSpec(name="static:calc", media_type="application/json", required=True)],
    )
    out = AgentTaskOutput(summary="x", outputs={"static:calc": echo})
    (result,) = [
        r
        for r in evaluate_task_checks(catalog, spec, out, arts, attempt_id="a1")
        if r.template_id == "artifact_present"
    ]
    assert result.status is ReqStatus.FAILED
    assert result.detail == "provenance_violation:static:calc"
    # Same for an unstamped (legacy) artifact: only "tool" passes a tool-id name.
    arts2 = LocalArtifactStore(tmp_path / "arts2")
    unstamped = _attempt_artifact(arts2, "a1", "static:calc", provenance=None)
    out2 = AgentTaskOutput(summary="x", outputs={"static:calc": unstamped})
    (result2,) = [
        r
        for r in evaluate_task_checks(catalog, spec, out2, arts2, attempt_id="a1")
        if r.template_id == "artifact_present"
    ]
    assert result2.status is ReqStatus.FAILED
    assert result2.detail == "provenance_violation:static:calc"


def test_default_agent_named_values_output_passes_with_producer_agent(tmp_path: Path) -> None:
    """Model-authored values stay allowed under NON-tool output names (producer=agent)."""
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    tool_handle = _attempt_artifact(arts, "a1", "static:calc", provenance="tool")
    agent_handle = _attempt_artifact(arts, "a1", "summary", provenance="agent")
    meta = arts.get_meta(agent_handle.artifact_id)
    assert meta is not None and meta.producer == "agent"
    spec = TaskSpec(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        tools_hint=["static:calc"],
        expected_outputs=[
            OutputSpec(name="static:calc", media_type="application/json", required=True),
            OutputSpec(name="summary", media_type="application/json", required=True),
        ],
    )
    out = AgentTaskOutput(summary="x", outputs={"static:calc": tool_handle, "summary": agent_handle})
    (result,) = [
        r
        for r in evaluate_task_checks(catalog, spec, out, arts, attempt_id="a1")
        if r.template_id == "artifact_present"
    ]
    assert result.status is ReqStatus.MET


def test_output_spec_rubric_artifact_exists_enforces_tool_provenance(tmp_path: Path) -> None:
    """Derive-attached per-output artifact_exists rubrics enforce the same policy.

    This is the path intent-derived plans take when the catalog has no
    experiment defaults: OutputSpec.rubric(code_kind=artifact_exists) named
    after the tools_hint tool id must be tool_call-backed.
    """
    from coscientist.core.claims.rubric_types import Rubric, RubricItem

    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")

    def _spec() -> TaskSpec:
        return TaskSpec(
            task_key="EXP-0",
            title="t",
            goal="g",
            route="experiment",
            tools_hint=["static:calc"],
            expected_outputs=[
                OutputSpec(
                    name="static:calc",
                    media_type="application/json",
                    required=True,
                    rubric=Rubric(
                        items=[
                            RubricItem(
                                id="exists",
                                text="required artifact exists",
                                required=True,
                                check="code",
                                code_kind="artifact_exists",
                            )
                        ],
                        aggregate="all_required",
                    ),
                )
            ],
        )

    echo = _attempt_artifact(arts, "a1", "static:calc", provenance="agent")
    results = evaluate_task_checks(
        catalog,
        _spec(),
        AgentTaskOutput(summary="x", outputs={"static:calc": echo}),
        arts,
        attempt_id="a1",
        task_id="t1",
        output_digest="d1",
    )
    rubric_results = [r for r in results if r.template_id.startswith("rubric:")]
    assert rubric_results and all(r.status is ReqStatus.FAILED for r in rubric_results)
    assert any("provenance_violation:static:calc" in (r.detail or "") for r in rubric_results)

    arts2 = LocalArtifactStore(tmp_path / "arts2")
    real = _attempt_artifact(arts2, "a1", "static:calc", provenance="tool")
    results2 = evaluate_task_checks(
        catalog,
        _spec(),
        AgentTaskOutput(summary="x", outputs={"static:calc": real}),
        arts2,
        attempt_id="a1",
        task_id="t1",
        output_digest="d1",
    )
    rubric_results2 = [r for r in results2 if r.template_id.startswith("rubric:")]
    assert rubric_results2 and all(r.status is ReqStatus.MET for r in rubric_results2)
