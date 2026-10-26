"""GOST builders — ТЗ (19.201-78) / НИР (7.32-2017) skeletons with prose slots."""

from __future__ import annotations

from coscientist.reporting.gost_tz import TzProse, TzSection, build_tz_markdown
from coscientist.reporting.graph_digest import build_graph_digest
from coscientist.reporting.nir import (
    NirOutlineSection,
    NirProse,
    build_nir_markdown,
    build_nir_outline,
)
from coscientist.reporting.science import ScienceReportProse, build_science_report_markdown


def _digest():
    nodes = [
        {
            "node_id": "st_1", "kind": "study", "study_id": "st_t",
            "attrs": {"request": "оценить токсичность", "goal": {"statement": "профиль"}},
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "node_id": "q_1", "kind": "question", "study_id": "st_t",
            "attrs": {"formulation": "какова токсичность?"},
            "created_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "node_id": "h_1", "kind": "hypothesis", "study_id": "st_t",
            "attrs": {"statement": "токсично", "prediction": "LD50 ниже 500", "selected": True,
                      "verdict": "refuted"},
            "created_at": "2026-01-01T00:00:02+00:00",
        },
        {
            "node_id": "x_1", "kind": "experiment", "study_id": "st_t",
            "attrs": {"task_key": "EXP-1", "title": "Расчёт", "goal": "рассчитать LD50",
                      "route": "experiment", "required": True,
                      "attempts": [{"n": 1, "outcome": "SUCCESS"}]},
            "created_at": "2026-01-01T00:00:03+00:00",
        },
        {
            "node_id": "a_1", "kind": "artifact", "study_id": "st_t",
            "attrs": {"artifact_id": "art_a", "role": "data", "media_type": "application/json",
                      "title": "таблица прогнозов"},
            "created_at": "2026-01-01T00:00:04+00:00",
        },
        {
            "node_id": "p_1", "kind": "artifact", "study_id": "st_t",
            "attrs": {"artifact_id": "art_p", "role": "paper", "title": "Статья о токсичности",
                      "doi_or_url": "https://doi.org/10.1000/x"},
            "created_at": "2026-01-01T00:00:05+00:00",
        },
        {
            "node_id": "cr_1", "kind": "criterion", "study_id": "st_t",
            "attrs": {"check": "code", "metric": "ld50", "op": "<", "target": "500"},
            "created_at": "2026-01-01T00:00:06+00:00",
        },
        {
            "node_id": "cn_1", "kind": "conclusion", "study_id": "st_t",
            "attrs": {"synthesis": "гипотеза опровергнута", "validity_bounds": "расчёт"},
            "created_at": "2026-01-01T00:00:07+00:00",
        },
    ]
    edges: list[dict[str, object]] = []
    return build_graph_digest("st_t", nodes, edges)


def _tz_prose() -> TzProse:
    return TzProse(
        topic="Оценка токсичности",
        sections=(
            TzSection(number="1", text="Работа выполняется на основании запроса заказчика."),
            TzSection(number="2", text="Целью работы является оценка токсичности."),
            TzSection(number="4", text="Предусматривается использование расчётных методов."),
            TzSection(number="6", text="Приёмка предусматривает проверку по критериям."),
        ),
    )


def test_tz_skeleton_order_and_graph_facts() -> None:
    md = build_tz_markdown(_digest(), _tz_prose())
    # Section order is numeric: 1, 2, 2.1, 3, 4, 5, 6.
    order = [line for line in md.splitlines() if line.startswith("## ")]
    assert order == [
        "## 1 Основание для разработки",
        "## 2 Цель и задача",
        "## 2.1 Задачи исследования (из графа, дословно)",
        "## 3 Требования к результату и критерии приёмки",
        "## 4 Требования к методам и средствам",
        "## 5 Состав работ (из графа)",
        "## 6 Порядок контроля и приёмки",
    ]
    # Tasks printed verbatim from the graph (question formulation, experiment goal).
    assert "какова токсичность?" in md
    assert "рассчитать LD50" in md
    # Criteria come from criterion nodes.
    assert "ld50 < 500" in md
    # Prose slots inserted.
    assert "Целью работы является" in md


def test_tz_missing_slot_renders_not_received() -> None:
    prose = TzProse(
        topic="",
        sections=(TzSection(number="1", text="основание"),),
    )
    md = build_tz_markdown(_digest(), prose)
    # Section 2 prose missing → «не получено», structure still complete.
    assert "## 2 Цель и задача" in md


def test_nir_outline_and_markdown() -> None:
    digest = _digest()
    outline = build_nir_outline(digest)
    assert outline.question == "какова токсичность?"
    assert outline.section_ids() == ("metodika", "proverka-gipotezy-pervaya", "rezultaty")
    prose = NirProse(
        research_title="Оценка токсичности соединения",
        abstract_text="Реферат работы.",
        keywords=("токсичность", "расчёт", "LD50", "порог", "модель"),
        introduction_paragraphs=("Актуальность…",),
        section_texts={"metodika": ("Методика…",), "rezultaty": ("Результаты…",)},
        section_titles={"metodika": "Методика", "rezultaty": "Обобщённые результаты"},
        conclusion_paragraphs=("Итог…",),
    )
    md = build_nir_markdown(digest, outline, prose)
    assert "# ОТЧЁТ О НАУЧНО-ИССЛЕДОВАТЕЛЬСКОЙ РАБОТЕ" in md
    assert "## Реферат" in md
    assert "## Содержание" in md
    assert "## Введение" in md
    assert "## 1 Методика" in md
    assert "## 2 " in md  # hypothesis section (title falls back to hint)
    assert "## 3 Обобщённые результаты" in md
    assert "## Заключение" in md
    # References built from paper artifacts (code, not model).
    assert "https://doi.org/10.1000/x" in md
    # Appendices list artifacts from the graph.
    assert "таблица прогнозов" in md
    # Missing section prose → «не получено» (hypothesis section has no text).
    assert "не получено" in md


def test_nir_outline_empty_digest() -> None:
    digest = build_graph_digest("st_t", [], [])
    outline = build_nir_outline(digest)
    assert outline.question == "не получено"
    assert outline.section_ids() == ("metodika", "rezultaty")


def test_science_report_structure() -> None:
    prose = ScienceReportProse(
        aim="цель работы",
        approach="подход",
        results="результаты",
        discussion="обсуждение",
        limitations="ограничения",
        next_step="шаг",
        materials="материалы",
    )
    md = build_science_report_markdown(_digest(), prose)
    for heading in (
        "## Цель", "## Подход", "## Результаты", "## Обсуждение",
        "## Ограничения", "## Следующий шаг", "## Материалы и воспроизводимость",
    ):
        assert heading in md
    # Code-appended artifact appendix (reproducibility refs from the graph).
    assert "Артефакт 1 —" in md


def test_nir_outline_section_model() -> None:
    section = NirOutlineSection(id="x", title_hint="т", digest_facts=("факт",))
    assert section.digest_facts == ("факт",)
