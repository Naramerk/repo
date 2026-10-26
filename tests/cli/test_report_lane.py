"""CLI report lane — post-finalize documents from the graph projection (e2e cassette)."""

from __future__ import annotations

import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy
from coscientist.llm.client import LlmClient
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import _cli_plan_out_json, _spec_json
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_T = TypeVar("_T")

_PROFILE = load_profile("research", expected_tool_catalog_version=1)
_MODEL = _PROFILE.roles["planner"].model


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
        del timeout_s
        return await awaitable

    return LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=LlmFakeClock(),
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )


def _science_body(text_extra: str = "") -> bytes:
    content = json.dumps(
        {
            "aim": "цель — оценить химическое пространство",
            "approach": "подход — два расчётных задания с инструментами",
            "results": "результаты — оба задания завершены" + text_extra,
            "discussion": "обсуждение — наблюдения согласованы",
            "limitations": "ограничения — только кассетные инструменты",
            "next_step": "следующий шаг — проверка на реальных данных",
            "materials": "материалы — результаты инструментов",
        },
        ensure_ascii=False,
    )
    return _ok_body(content=content, prompt_tokens=6, completion_tokens=4)


def _tz_body() -> bytes:
    content = json.dumps(
        {
            "topic": "Исследование химического пространства",
            "sections": [
                {"number": "1", "text": "Работа выполняется на основании запроса заказчика."},
                {"number": "2", "text": "Целью работы является исследование пространства."},
                {"number": "4", "text": "Предусматривается использование расчётных инструментов."},
                {"number": "6", "text": "Приёмка предусматривает проверку результатов."},
            ],
        },
        ensure_ascii=False,
    )
    return _ok_body(content=content, prompt_tokens=6, completion_tokens=4)


def _nir_body() -> bytes:
    content = json.dumps(
        {
            "research_title": "Исследование химического пространства",
            "abstract_text": "Отчёт содержит результаты расчётного исследования.",
            "keywords": ["химическое пространство", "расчёт", "кластеризация", "данные", "отчёт"],
            "introduction_paragraphs": ["Актуальность работы определяется запросом."],
            "section_texts": {"metodika": ["Применены расчётные инструменты."]},
            "section_titles": {"metodika": "Методика исследования"},
            "conclusion_paragraphs": ["Задачи исследования выполнены."],
        },
        ensure_ascii=False,
    )
    return _ok_body(content=content, prompt_tokens=6, completion_tokens=4)


def _study_bodies() -> list[bytes]:
    return [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2
        ),
    ]


def _doc_bodies() -> list[bytes]:
    return [_science_body(), _tz_body(), _nir_body()]


def _arts_meta(data_root: Path, study_id: str) -> dict[str, str]:
    """artifact name → markdown body (via meta JSON + CAS blob)."""
    out: dict[str, str] = {}
    meta_dir = data_root / study_id / "arts" / "meta"
    for meta_file in sorted(meta_dir.glob("*.json")):
        row = json.loads(meta_file.read_text(encoding="utf-8"))
        name = str(row["name"])
        sha = str(row["sha256"])
        out[name] = (data_root / study_id / "arts" / "cas" / sha).read_text(encoding="utf-8")
    return out


async def _run_with_report(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    study_id: str = "report_lane_study",
    tool_results: dict[str, dict[str, object]] | None = None,
) -> tuple[int, dict[str, object]]:
    task = tmp_path / "task.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(
        tools=tool_results
        or {
            "dataset_overview": {"result": {"ok": True}, "args": {}},
            "chemical_space_clustering": {"result": {"ok": True}, "args": {}},
        }
    )
    client = _client(bodies, monkeypatch=monkeypatch)
    adapter = LlmAgentAdapter(
        client,
        role_models={
            "planner": _MODEL,
            "executor-route": _MODEL,
            "tz_spec": _MODEL,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=_PROFILE,
        owner="report-lane-test",
        adapter=adapter,
        out=out,
        study_id=study_id,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        finalize_reserve_s=30,
        hitl_policy=HitlPolicy(mode="headless"),
        lease_ttl_s=60.0,
        max_llm_calls=100,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        tools=router,
        wired_roles=frozenset({"report"}),
        study_clock=FakeClock(),
        llm_client=client,
    )
    summary = json.loads(out.getvalue().strip())
    return code, summary


@pytest.mark.asyncio
async def test_report_lane_writes_three_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fulfilled run + wired report → science/ТЗ/НИР in CAS beside finalize report.md."""
    code, summary = await _run_with_report(
        tmp_path, monkeypatch=monkeypatch, bodies=[*_study_bodies(), *_doc_bodies()]
    )
    assert code == 0
    arts = _arts_meta(tmp_path / "data", "report_lane_study")
    # The technical report.md from finalize stays alongside; the lane adds three
    # (plus the executor tool-result artifacts from the run itself).
    assert {
        "report.md",
        "report.science.md",
        "tz.gost-19.201-78.md",
        "nir.gost-7.32-2017.md",
    } <= set(arts)
    assert "## Цель" in arts["report.science.md"]
    assert "ТЕХНИЧЕСКОЕ ЗАДАНИЕ" in arts["tz.gost-19.201-78.md"]
    assert "ОТЧЁТ О НАУЧНО-ИССЛЕДОВАТЕЛЬСКОЙ РАБОТЕ" in arts["nir.gost-7.32-2017.md"]
    # Usage row records the lane honestly.
    report_rows = [u for u in summary["usage"] if u.get("role") == "report"]  # type: ignore[union-attr]
    assert len(report_rows) == 1
    assert set(report_rows[0]["documents"]) == {  # type: ignore[index]
        "report.science.md",
        "tz.gost-19.201-78.md",
        "nir.gost-7.32-2017.md",
    }


@pytest.mark.asyncio
async def test_report_lane_untraceable_number_refuses_after_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invented numbers → one bounded repair round → typed refuse (code 8)."""
    bodies = [
        *_study_bodies(),
        _science_body(text_extra="; точность составила 99 процентов"),
        _science_body(text_extra="; точность составила 98 процентов"),
    ]
    with pytest.raises(CliRefuse, match="report lane refused") as ei:
        await _run_with_report(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 8
    # The refuse carries the untraceable number of the LAST (repair) attempt.
    assert "98" in ei.value.detail
    # No lane artifacts written (finalize report.md still is).
    arts = _arts_meta(tmp_path / "data", "report_lane_study")
    assert "report.md" in arts
    assert not (
        {"report.science.md", "tz.gost-19.201-78.md", "nir.gost-7.32-2017.md"} & set(arts)
    )


@pytest.mark.asyncio
async def test_report_lane_repair_round_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First body has an invented number; the repair body passes → documents land."""
    bodies = [
        *_study_bodies(),
        _science_body(text_extra="; точность составила 97 процентов"),
        _science_body(),
        _tz_body(),
        _nir_body(),
    ]
    code, _ = await _run_with_report(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert code == 0
    arts = _arts_meta(tmp_path / "data", "report_lane_study")
    assert "report.science.md" in arts
    assert "точность составила 97" not in arts["report.science.md"]


@pytest.mark.asyncio
async def test_report_lane_tool_numbers_trace_to_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 evidence-link: real tool-result numbers (225/132/7) land in the
    science report traced to journaled evidence — no «не получено» wall."""
    tool_results = {
        "dataset_overview": {"result": {"n_rows": 225, "n_cols": 132}, "args": {}},
        "chemical_space_clustering": {"result": {"n_clusters": 7}, "args": {}},
    }
    science = json.dumps(
        {
            "aim": "цель — оценить химическое пространство",
            "approach": "подход — два расчётных задания с инструментами",
            "results": "результаты — выборка 225 строк и 132 признака, выделено 7 кластеров",
            "discussion": "обсуждение — наблюдения согласованы",
            "limitations": "ограничения — только кассетные инструменты",
            "next_step": "следующий шаг — проверка на реальных данных",
            "materials": "материалы — результаты инструментов обзора и кластеризации",
        },
        ensure_ascii=False,
    )
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2
        ),
        _ok_body(content=science, prompt_tokens=6, completion_tokens=4),
        _tz_body(),
        _nir_body(),
    ]
    code, _ = await _run_with_report(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        study_id="evidence_link_study",
        tool_results=tool_results,
    )
    assert code == 0
    arts = _arts_meta(tmp_path / "data", "evidence_link_study")
    md = arts["report.science.md"]
    # The tool numbers are in the document...
    for number in ("225", "132"):
        assert number in md
    # ...and the materials appendix carries the journaled evidence facts
    # (evidence nodes derived_from the tool artifacts) — provenance visible.
    assert "Наблюдение 1" in md
    assert "n_rows = 225" in md
    assert "n_cols = 132" in md
    assert "Артефакт 1" in md
