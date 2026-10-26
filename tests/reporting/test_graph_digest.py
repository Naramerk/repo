"""Graph digest builder — nodes/edges → digest, numbers inventory, refuses."""

from __future__ import annotations

import pytest

from coscientist.reporting.graph_digest import DigestRefuse, build_graph_digest


def _node(
    node_id: str,
    kind: str,
    attrs: dict[str, object],
    created: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": kind,
        "study_id": "st_test",
        "attrs": attrs,
        "created_at": created,
    }


def _edge(edge_id: str, kind: str, src: str, dst: str) -> dict[str, object]:
    return {
        "edge_id": edge_id,
        "kind": kind,
        "study_id": "st_test",
        "src_id": src,
        "dst_id": dst,
        "created_at": "2026-01-01T00:00:01+00:00",
    }


def _study_graph() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    nodes = [
        _node("st_1", "study", {
            "request": "оценить токсичность",
            "goal": {"statement": "профиль токсичности", "research_form": None, "trl": None},
            "constraints": [{"subtype": "resource", "content": "только открытые данные", "limit": None}],
            "profile": "research",
            "catalog_version": 1,
            "confirmations_needed": 1,
            "max_expansions": 2,
            "max_attempts": 3,
        }),
        _node("q_1", "question", {"formulation": "какова токсичность?", "gap": "нет данных"}),
        _node("h_1", "hypothesis", {
            "statement": "соединение токсично",
            "prediction": "LD50 ниже 500 мг/кг",
            "selected": True,
            "verdict": "refuted",
        }),
        _node("x_1", "experiment", {
            "task_key": "EXP-1",
            "title": "Расчёт показателей",
            "goal": "рассчитать LD50",
            "route": "experiment",
            "required": True,
            "attempts": [
                {"n": 1, "outcome": "SUCCESS", "close_reason": "result", "provenance": "real"},
            ],
        }),
        _node("e_1", "evidence", {
            "kind": "computational",
            "statement": "расчётное LD50 = 612.5 мг/кг",
            "ref": {"artifact_id": "art_x", "json_pointer": "/ld50"},
        }),
        _node("a_1", "artifact", {
            "artifact_id": "art_x",
            "role": "data",
            "media_type": "application/json",
            "title": "таблица прогнозов",
        }),
        _node("cl_1", "claim", {
            "target": "h_1",
            "checked_against": "cr_1",
            "stance": "refutes",
            "reason": "порог не достигнут",
            "criterion_status": "not_met",
            "p_value": 0.03,
        }),
        _node("cn_1", "conclusion", {
            "synthesis": "гипотеза опровергнута",
            "validity_bounds": "расчётные данные",
        }),
        _node("cr_1", "criterion", {
            "check": "code",
            "metric": "ld50",
            "op": "<",
            "target": "500",
            "tolerance": 0.05,
            "alpha": 0.05,
        }),
    ]
    edges = [
        _edge("ed1", "tests", "x_1", "h_1"),
        _edge("ed2", "cites", "cl_1", "e_1"),
        _edge("ed3", "about", "cl_1", "h_1"),
        _edge("ed4", "produced", "x_1", "a_1"),
        _edge("ed5", "derived_from", "e_1", "a_1"),
        _edge("ed6", "answers", "cn_1", "q_1"),
    ]
    return nodes, edges


def test_digest_labels_and_edges_no_internal_ids() -> None:
    digest = build_graph_digest("st_test", *_study_graph())
    assert digest.labels["h_1"] == "Гипотеза 1"
    assert digest.labels["q_1"] == "Вопрос 1"
    assert digest.labels["x_1"] == "Эксперимент 1"
    assert digest.labels["e_1"] == "Наблюдение 1"
    assert digest.labels["cn_1"] == "Вывод 1"
    # Refuted hypothesis is stated as-is (no softening).
    assert "опровергнута" in digest.markdown
    assert "Статус: опровергнута" in digest.markdown
    # Edge rendered as a relation between labels.
    assert "Эксперимент 1 проверяет Гипотезу 1." in digest.markdown
    # Facts by label carry the digest content.
    assert any("LD50 ниже 500" in fact for fact in digest.facts_by_label["Гипотеза 1"])
    # No internal ids in the digest markdown itself.
    for token in ("h_1", "x_1", "cl_1", "EXP-1", "art_x"):
        assert token not in digest.markdown


def test_digest_numbers_inventory() -> None:
    digest = build_graph_digest("st_test", *_study_graph())
    numbers = digest.number_tokens()
    # Numbers present in graph facts are in the inventory.
    assert "612.5" in numbers
    assert "500" in numbers
    assert "0.03" in numbers
    assert "0.05" in numbers
    # Computed counts from the graph are code facts.
    assert "1" in numbers  # labels / всего вопросов
    assert "11" not in numbers  # invented numbers are not there


def test_digest_missing_renders_not_received() -> None:
    nodes = [_node("q_1", "question", {"formulation": ""})]
    digest = build_graph_digest("st_test", nodes, [])
    assert "не получено" in digest.markdown
    digest2 = build_graph_digest("st_test", [], [])
    assert "Цель: не получено" in digest2.markdown


def test_digest_skips_machinery_kinds() -> None:
    nodes = [
        _node("tc_1", "tool_call", {"call_id": "c1", "tool": "t", "duration_ms": 5}),
        _node("q_1", "question", {"formulation": "вопрос"}),
    ]
    digest = build_graph_digest("st_test", nodes, [])
    assert "tool_call" not in digest.labels.values()
    assert "Вопрос 1" in digest.markdown


def test_digest_foreign_study_row_refuses() -> None:
    nodes = [_node("q_1", "question", {"formulation": "x"})]
    nodes[0]["study_id"] = "st_other"
    with pytest.raises(DigestRefuse, match="study_id"):
        build_graph_digest("st_test", nodes, [])


def test_digest_malformed_row_refuses() -> None:
    with pytest.raises(DigestRefuse, match="graph_nodes row invalid"):
        build_graph_digest("st_test", [{"node_id": "", "kind": "question", "study_id": "st_test", "attrs": {}}], [])
    with pytest.raises(DigestRefuse, match="graph_edges row invalid"):
        build_graph_digest("st_test", [], [{"edge_id": "e", "kind": "tests", "study_id": "st_test"}])


def test_digest_hypothesis_verdict_words() -> None:
    nodes = [
        _node("h_1", "hypothesis", {"statement": "а", "prediction": "б", "selected": True, "verdict": "supported"}),
        _node("h_2", "hypothesis", {"statement": "в", "prediction": "г", "selected": True, "verdict": "inconclusive"}),
    ]
    digest = build_graph_digest("st_test", nodes, [])
    assert "подтверждена" in digest.markdown
    assert "не разрешена имеющимися данными" in digest.markdown
    assert "Гипотез со статусом" in digest.markdown
