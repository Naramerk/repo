"""C1 evidence-link: journaled tool_call artifacts → evidence graph nodes.

Covers: mechanical JSON facts extraction (typed caps), reference-only
fallbacks (non-JSON / oversized / non-finite), G7 event construction
(artifact + evidence nodes, derived_from / produced / relates_to edges),
the digest carrying extracted numbers, honesty tracing, and the executor
record path journaling linkage in the ResultRecorded batch.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.claims.graph import GraphNode, GraphStore, registry_from_schema
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.ids import experiment_node_id, question_node_id
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CheckResult,
    EvidenceRef,
    ReqStatus,
    TaskState,
)
from coscientist.core.execution.evidence_link import (
    artifact_node_id,
    evidence_link_events,
    evidence_node_id,
)
from coscientist.core.execution.fact_projection import DEFAULT_LIMITS
from coscientist.reporting.graph_digest import build_graph_digest
from coscientist.reporting.honesty import DigestFacts, validate_prose
from coscientist.reporting.science import ScienceReportProse, build_science_report_markdown
from tests.helpers import attach_executor, close_runtime

_SID = "st_el"
_NOW = FakeClock().now()


def _tool_artifact(
    arts: LocalArtifactStore,
    content: bytes,
    *,
    name: str = "dataset_overview",
    call: str = "st_el/EXP-1#1:dataset_overview",
):
    # Mirror cli/journal.py record_executor_route: the CLI journal stamps
    # provenance="tool" (bytes ARE the attempt's recorded tool_call result).
    return arts.put(
        content,
        make_tool_ref(call, tool=name),
        name=name,
        media_type="application/json",
        provenance="tool",
    )


def _agent_artifact(arts: LocalArtifactStore, content: bytes, *, name: str = "main"):
    return arts.put(
        content,
        make_tool_ref(f"{name}-call"),
        name=name,
        media_type="application/json",
    )


def _task(addresses: tuple[str, ...] = ("Q1",)):
    from coscientist.core.execution.domain import RequirementRef, Task, TaskKind, TaskSpec

    spec = TaskSpec(
        task_key="EXP-1",
        title="Обзор данных",
        goal="посчитать строки",
        route="experiment",
        depends_on=["PLAN"],
        criteria=[RequirementRef(template_id="value_match")],
        addresses=list(addresses),
    )
    return Task(
        task_id=f"{_SID}/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.RUNNING,
        deadline=_NOW + timedelta(seconds=600),
    )


def _graph_with(*, question: bool = True, experiment: bool = True) -> GraphStore:
    store = GraphStore(registry_from_schema())
    if question:
        store.upsert_node(
            GraphNode(
                node_id=question_node_id(_SID, "Q1"),
                kind="question",
                study_id=_SID,
                ref_id="Q1",
                attrs={"formulation": "каков объём данных?"},
            )
        )
    if experiment:
        store.upsert_node(
            GraphNode(
                node_id=experiment_node_id(_SID, "EXP-1"),
                kind="experiment",
                study_id=_SID,
                ref_id="EXP-1",
            )
        )
    return store


def _events_by_type(events: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for ev in events:
        out.setdefault(ev.type, []).append(ev)
    return out


# --- evidence_link_events: G7 construction -----------------------------------


def test_link_journals_evidence_with_facts_and_edges(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    art = _tool_artifact(arts, json.dumps({"n_rows": 225, "n_cols": 132}).encode())
    graph = _graph_with()
    task = _task()
    events = evidence_link_events(
        study_id=_SID,
        task=task,
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"dataset_overview": ArtifactHandle(artifact_id=art.artifact_id, name="dataset_overview")},
        artifacts=arts,
        now=_NOW,
        graph=graph,
    )
    by_type = _events_by_type(events)
    assert [ev.type for ev in events].count("GraphNodeUpserted") == 4
    assert [ev.type for ev in events].count("GraphEdgeAdded") == 7
    art_ev = by_type["GraphNodeUpserted"][0].payload
    ev_ev = by_type["GraphNodeUpserted"][1].payload
    assert art_ev["kind"] == "artifact"
    assert art_ev["attrs"]["artifact_id"] == art.artifact_id
    assert art_ev["attrs"]["role"] == "data"
    assert art_ev["attrs"]["sha256"] == art.sha256
    assert ev_ev["kind"] == "evidence"
    assert ev_ev["node_id"] == evidence_node_id(_SID, art.artifact_id)
    assert ev_ev["attrs"]["kind"] == "computational"
    facts = {
        ev.payload["attrs"]["ref"]["json_pointer"]: ev.payload["attrs"]["statement"]
        for ev in by_type["GraphNodeUpserted"][2:]
    }
    assert facts["/n_rows"] == '"/n_rows" = 225'
    assert facts["/n_cols"] == '"/n_cols" = 132'
    assert "покрытие=complete" in ev_ev["attrs"]["statement"]
    assert ev_ev["attrs"]["ref"]["digest"] == art.sha256
    assert ev_ev["attrs"]["ref"]["artifact_id"] == art.artifact_id
    assert ev_ev["attrs"]["artifact_uri"] == art.uri
    edge_kinds = sorted(
        (ev.payload["kind"], ev.payload["src_id"], ev.payload["dst_id"]) for ev in by_type["GraphEdgeAdded"]
    )
    assert (
        "derived_from",
        evidence_node_id(_SID, art.artifact_id),
        artifact_node_id(_SID, art.artifact_id),
    ) in edge_kinds
    assert (
        "produced",
        experiment_node_id(_SID, "EXP-1"),
        artifact_node_id(_SID, art.artifact_id),
    ) in edge_kinds
    assert (
        "relates_to",
        evidence_node_id(_SID, art.artifact_id),
        question_node_id(_SID, "Q1"),
    ) in edge_kinds


def test_link_non_json_tool_artifact_is_reference_only(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    art = _tool_artifact(arts, b"\x00\x01binary-not-json", name="raw_export")
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"raw_export": ArtifactHandle(artifact_id=art.artifact_id, name="raw_export")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(),
    )
    ev_payloads = [ev.payload for ev in events if ev.type == "GraphNodeUpserted" and ev.payload["kind"] == "evidence"]
    assert len(ev_payloads) == 1
    statement = ev_payloads[0]["attrs"]["statement"]
    # Reference-only: no invented content — locator facts only.
    assert "покрытие=reference" in statement
    assert f"{art.size} байт" in statement
    assert ev_payloads[0]["attrs"]["ref"]["digest"] == art.sha256


def test_link_agent_echo_artifacts_are_excluded(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    art = _agent_artifact(arts, b'{"count": 225}')
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(),
    )
    assert events == []


def test_link_missing_question_or_experiment_nodes_skip_edges(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    art = _tool_artifact(arts, b'{"n": 1}')
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"dataset_overview": ArtifactHandle(artifact_id=art.artifact_id, name="dataset_overview")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(question=False, experiment=False),
    )
    kinds = [ev.payload["kind"] for ev in events if ev.type == "GraphEdgeAdded"]
    assert kinds == ["derived_from", "derived_from"]


def test_link_unregistered_artifact_skips_without_raising(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"ghost": ArtifactHandle(artifact_id="art_missing", name="ghost")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(),
    )
    assert events == []


def test_link_oversized_json_artifact_is_reference_only(tmp_path: Path) -> None:
    """Even valid JSON over the parse cap stays reference-only (no facts)."""
    arts = LocalArtifactStore(tmp_path / "arts")
    big = json.dumps({"blob": "x" * (DEFAULT_LIMITS.max_bytes + 1)}).encode()
    art = _tool_artifact(arts, big, name="big_dump")
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"big_dump": ArtifactHandle(artifact_id=art.artifact_id, name="big_dump")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(),
    )
    ev_payloads = [ev.payload for ev in events if ev.type == "GraphNodeUpserted" and ev.payload["kind"] == "evidence"]
    assert len(ev_payloads) == 1
    statement = ev_payloads[0]["attrs"]["statement"]
    assert "byte_budget" in statement
    assert "blob" not in statement  # no content leak — locator facts only
    assert f"{art.size} байт" in statement


# --- digest + honesty: numbers trace to evidence ------------------------------


def _digest_fixture_rows() -> tuple[list[dict], list[dict]]:
    art_nid = artifact_node_id(_SID, "art_x")
    ev_nid = evidence_node_id(_SID, "art_x")
    nodes = [
        {
            "node_id": ev_nid,
            "kind": "evidence",
            "study_id": _SID,
            "attrs": {
                "kind": "computational",
                "statement": "Результат инструмента dataset_overview: n_rows = 225; n_cols = 132",
                "ref": {"artifact_id": "art_x", "json_pointer": "", "digest": "a" * 64},
            },
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "node_id": art_nid,
            "kind": "artifact",
            "study_id": _SID,
            "attrs": {
                "artifact_id": "art_x",
                "role": "data",
                "media_type": "application/json",
                "title": "dataset_overview",
            },
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ]
    edges = [
        {
            "edge_id": f"derived_from:{ev_nid}:{art_nid}",
            "kind": "derived_from",
            "study_id": _SID,
            "src_id": ev_nid,
            "dst_id": art_nid,
            "created_at": "2026-01-01T00:00:01+00:00",
        }
    ]
    return nodes, edges


def test_digest_carries_evidence_facts_and_materials_trace() -> None:
    nodes, edges = _digest_fixture_rows()
    digest = build_graph_digest(_SID, nodes, edges)
    assert "Наблюдение 1" in digest.markdown
    assert "n_rows = 225" in digest.markdown
    assert "Артефакт 1" in digest.markdown
    # Materials trace: evidence label linked to the artifact label.
    assert digest.evidence_for_artifact("Артефакт 1") == ("Наблюдение 1",)
    assert "Наблюдение 1 получено из Артефакта 1." in digest.markdown
    # Numbers from the journaled artifact bytes are in the digest inventory.
    assert "225" in digest.number_tokens()
    assert "132" in digest.number_tokens()


def test_honesty_prose_traces_numbers_to_evidence_facts() -> None:
    nodes, edges = _digest_fixture_rows()
    digest = build_graph_digest(_SID, nodes, edges)
    facts = DigestFacts(digest.markdown)
    honest = "Выборка содержит 225 строк и 132 признака."
    assert validate_prose(honest, facts) == ()
    invented = "Выборка содержит 999 строк."
    violations = validate_prose(invented, facts)
    assert violations and "999" in violations[0]


def test_science_materials_section_lists_evidence_facts() -> None:
    nodes, edges = _digest_fixture_rows()
    digest = build_graph_digest(_SID, nodes, edges)
    prose = ScienceReportProse(
        aim="цель",
        approach="подход",
        results="результаты: 225 строк",
        discussion="обсуждение",
        limitations="ограничения",
        next_step="шаг",
        materials="материалы — инструменты обзора данных",
    )
    report = build_science_report_markdown(digest, prose)
    assert "Артефакты исследования (по графу):" in report
    assert "- Артефакт 1 — Название: dataset_overview" in report
    assert "- Наблюдение 1: [расчётное] Наблюдение: Результат инструмента dataset_overview: n_rows = 225" in report


# --- executor record path journals the linkage --------------------------------


@pytest.mark.asyncio
async def test_executor_record_journals_evidence_link(tmp_path: Path, clock: FakeClock) -> None:
    from tests.test_transitions import _boot

    rt = await _boot(tmp_path, clock, runtime_cls=None)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    # count satisfies the catalog value_match (/count == 225); n_rows/n_cols
    # are the tool's own facts — the evidence statement carries all leaves.
    # The call ref must bind to THIS attempt (artifact_belongs_to_attempt).
    art = _tool_artifact(
        arts,
        json.dumps({"count": 225, "n_rows": 225, "n_cols": 132}).encode(),
        call=f"{aid}:dataset_overview",
    )
    from coscientist.core.execution.domain import RequirementRef

    task_id = rt.attempts[aid]["task_id"]
    task = rt.tasks[task_id]
    if not task.spec.criteria:
        new_spec = task.spec.model_copy(update={"criteria": [RequirementRef(template_id="value_match")]})
        rt.tasks[task.task_id] = task.model_copy(update={"spec": new_spec})
    result = await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="dataset_overview")},
            values={"count": 225},
        ),
    )
    assert result.outcome.value == "SUCCESS"
    nodes = {
        r["node_id"]: r for r in await rt.store.get_projection("graph_nodes") if isinstance(r, dict) and "node_id" in r
    }
    edges = await rt.store.get_projection("graph_edges")
    ev_nid = evidence_node_id(rt.study.study_id, art.artifact_id)
    art_nid = artifact_node_id(rt.study.study_id, art.artifact_id)
    assert ev_nid in nodes and nodes[ev_nid]["kind"] == "evidence"
    observed = {
        r["attrs"]["ref"]["json_pointer"]: r["attrs"]["statement"] for r in nodes.values() if r["kind"] == "evidence"
    }
    assert observed["/n_rows"] == '"/n_rows" = 225'
    assert art_nid in nodes and nodes[art_nid]["kind"] == "artifact"
    edge_kinds = [e["kind"] for e in edges if isinstance(e, dict)]
    assert "derived_from" in edge_kinds
    await close_runtime(rt)


def test_required_contrary_observation_survives_large_payload_and_report_display(tmp_path):
    arts = LocalArtifactStore(tmp_path / "arts")
    payload = {"rows": [{"response": i, "annotation": "sample " * 20} for i in range(200)]}
    payload["rows"][-1]["response"] = 987654321
    raw = json.dumps(
        {
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
                "isError": False,
            },
            "content_digest": "a" * 64,
        }
    ).encode()
    assert len(raw) > 10_240
    art = _tool_artifact(arts, raw, name="measurement")
    pointer = "/result/structuredContent/rows/199/response"
    receipt = CheckResult(
        check_id="measurement#0",
        template_id="measurement",
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        role="prediction",
        effect={"est": 999999999},
        evidence=[EvidenceRef(artifact_id=art.artifact_id, json_pointer=pointer, digest="b" * 64)],
    )
    events = evidence_link_events(
        study_id=_SID,
        task=_task(),
        attempt_id=f"{_SID}/EXP-1#1",
        outputs={"measurement": ArtifactHandle(artifact_id=art.artifact_id, name="measurement")},
        artifacts=arts,
        now=_NOW,
        graph=_graph_with(),
        check_results=[receipt],
    )
    nodes = [ev.payload for ev in events if ev.type == "GraphNodeUpserted"]
    edges = [ev.payload for ev in events if ev.type == "GraphEdgeAdded"]
    observations = [n for n in nodes if n["kind"] == "evidence"]
    fact = next(n for n in observations if n["attrs"]["ref"]["json_pointer"] == pointer)
    assert fact["attrs"]["ref"]["digest"] == art.sha256
    assert fact["attrs"]["statement"].endswith("= 987654321")
    assert "999999999" not in str(observations)
    assert all(len(n["attrs"]["statement"]) < 600 for n in observations)
    digest = build_graph_digest(_SID, nodes, edges)
    assert len(digest.markdown) > 10_240
    assert "987654321" in digest.number_tokens()
    assert "999999999" not in digest.number_tokens()
    assert pointer in digest.markdown
    prose = ScienceReportProse(
        aim="наблюдение",
        approach="измерение",
        results="Наблюдаемое значение 987654321.",
        discussion="Без подтверждения гипотезы.",
        limitations="Неполная проекция остальных строк.",
        next_step="Проверить остальные данные.",
        materials="Зарегистрированный результат.",
    )
    report = build_science_report_markdown(digest, prose)
    assert "987654321" in report and pointer in report
    assert validate_prose("Измерено 987654321.", DigestFacts(digest.markdown)) == ()


def test_cas_corruption_cannot_become_an_observed_fact(tmp_path):
    arts = LocalArtifactStore(tmp_path / "arts")
    art = _tool_artifact(arts, b'{"observed":42}')
    (tmp_path / "arts" / "cas" / art.sha256).write_bytes(b'{"observed":43}')
    with pytest.raises(Exception, match="digest|sha256|hash"):
        evidence_link_events(
            study_id=_SID,
            task=_task(),
            attempt_id=f"{_SID}/EXP-1#1",
            outputs={"measurement": ArtifactHandle(artifact_id=art.artifact_id, name="measurement")},
            artifacts=arts,
            now=_NOW,
            graph=_graph_with(),
        )
