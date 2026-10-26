"""Honesty validator — untraceable numbers, internal ids, register words."""

from __future__ import annotations

from coscientist.reporting.honesty import (
    DigestFacts,
    ReportHonestyRefuse,
    extract_number_tokens,
    validate_prose,
)

_DIGEST = """# Дайджест исследования (по графу)
## Вопросы
- Вопрос 1: Формулировка: какова токсичность?
## Гипотезы
- Гипотеза 1: Проверяемый прогноз: LD50 ниже 500 мг/кг
## Наблюдения
- Наблюдение 1: [расчётное] Наблюдение: расчётное LD50 = 612.5 мг/кг
"""


def test_untraceable_number_refuses() -> None:
    facts = DigestFacts(_DIGEST)
    text = "получено значение 42 мг/кг"
    violations = validate_prose(text, facts)
    assert any("42" in v for v in violations)


def test_traceable_number_passes() -> None:
    facts = DigestFacts(_DIGEST)
    assert validate_prose("расчётное LD50 составило 612.5 мг/кг при пороге 500", facts) == ()


def test_refuted_hypothesis_wording_passes() -> None:
    facts = DigestFacts(_DIGEST)
    assert validate_prose("гипотеза опровергнута: порог не достигнут", facts) == ()


def test_not_received_passes() -> None:
    facts = DigestFacts(_DIGEST)
    assert validate_prose("данные о мощности не получено", facts) == ()


def test_english_status_word_refuses() -> None:
    facts = DigestFacts(_DIGEST)
    violations = validate_prose("гипотеза refuted по критерию", facts)
    assert any("refuted" in v for v in violations)


def test_agent_name_refuses() -> None:
    facts = DigestFacts(_DIGEST)
    violations = validate_prose("планировщик planner выбрал маршрут", facts)
    assert any("planner" in v for v in violations)


def test_internal_id_refuses_but_digest_sourced_name_passes() -> None:
    facts = DigestFacts(_DIGEST)
    assert any("H1" in v for v in validate_prose("гипотеза H1 подтверждена", facts))
    assert any("TASK-3" in v for v in validate_prose("задача TASK-3 закрыта", facts))
    assert any("cn_" in v for v in validate_prose("вывод cn_ab12cd34ef56 хранится", facts))
    # LD50 exists in the digest facts — a graph-sourced name, not an id.
    assert validate_prose("LD50 ниже порога", facts) == ()


def test_gost_standard_number_not_a_numeric_claim() -> None:
    facts = DigestFacts(_DIGEST)
    # 7.32-2017 is glued to the standard designation, not a standalone number.
    assert "7.32" not in extract_number_tokens("по ГОСТ 7.32-2017 оформляют")
    assert validate_prose("оформление по ГОСТ 7.32-2017", facts) == ()


def test_list_markers_are_structure() -> None:
    facts = DigestFacts(_DIGEST)
    text = "1. первый шаг\n2. второй шаг\n"
    assert validate_prose(text, facts) == ()


def test_number_normalization() -> None:
    assert extract_number_tokens("5,5 раза; 0.05; 007") == frozenset({"5.5", "0.05", "7"})
    assert extract_number_tokens("LD50 и file.png") == frozenset()


def test_refuse_type_exists() -> None:
    exc = ReportHonestyRefuse("detail")
    assert exc.detail == "detail"
