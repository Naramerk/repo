"""ТЗ по ГОСТ 19.201-78 — code-built skeleton, model fills prose slots.

Register follows the legacy CoScientist tz_spec agent (facts already fixed by
the confirmed frame; the model only restates them in official impersonal
Russian). Tasks, criteria and the work breakdown are printed from the graph
digest verbatim by code — the model never rewrites them. Numbers in prose
must trace to the digest (honesty gate).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from coscientist.reporting.graph_digest import GraphDigest

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)

_MISSING = "не получено"

# Prose sections the model fills (number → standard heading).
PROSE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("1", "Основание для разработки"),
    ("2", "Цель и задача"),
    ("4", "Требования к методам и средствам"),
    ("6", "Порядок контроля и приёмки"),
)


class TzSection(BaseModel):
    model_config = _FROZEN

    number: StrictStr = Field(min_length=1)
    text: StrictStr = Field(min_length=1)


class TzProse(BaseModel):
    """Model output: topic (optional) + prose for sections 1/2/4/6 only."""

    model_config = _FROZEN

    topic: StrictStr = ""
    sections: tuple[TzSection, ...] = Field(min_length=1)

    def text_for(self, number: str) -> str:
        for section in self.sections:
            if section.number == number:
                return section.text.strip()
        return _MISSING

    def prose_texts(self) -> tuple[str, ...]:
        return tuple(section.text for section in self.sections)


def _tasks_lines(digest: GraphDigest) -> list[str]:
    """Задачи исследования — verbatim graph facts (questions then experiments)."""
    lines: list[str] = []
    for label in digest.question_labels():
        formulation = ""
        for fact in digest.facts_by_label.get(label, ()):
            if fact.startswith("Формулировка: "):
                formulation = fact.removeprefix("Формулировка: ")
                break
        lines.append(f"- {formulation}" if formulation else f"- {_MISSING}")
    for label in digest.experiment_labels():
        goal = ""
        title = ""
        for fact in digest.facts_by_label.get(label, ()):
            if fact.startswith("Цель задачи: "):
                goal = fact.removeprefix("Цель задачи: ")
            elif fact.startswith("Название: "):
                title = fact.removeprefix("Название: ")
        body = goal if goal else (title if title else _MISSING)
        lines.append(f"- {body}")
    return lines


def _criteria_lines(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label, facts in digest.facts_by_label.items():
        if not label.startswith("Критерий "):
            continue
        body = "; ".join(facts)
        lines.append(f"- {body}" if body else f"- {_MISSING}")
    return lines


def _composition_lines(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label in digest.experiment_labels():
        parts = [fact for fact in digest.facts_by_label.get(label, ()) if fact]
        body = "; ".join(parts)
        lines.append(f"- {body}" if body else f"- {_MISSING}")
    return lines


def _constraints_lines(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label, facts in digest.facts_by_label.items():
        if not label.startswith("Ограничение "):
            continue
        body = "; ".join(facts)
        lines.append(f"- {body}" if body else f"- {_MISSING}")
    return lines


def _bullet_block(lines: list[str]) -> str:
    if not lines:
        return _MISSING
    return "\n".join(lines)


def build_tz_markdown(digest: GraphDigest, prose: TzProse) -> str:
    """Assemble the ТЗ document (skeleton + graph facts by code, prose by model)."""
    topic = prose.topic.strip()
    header = [
        "# ТЕХНИЧЕСКОЕ ЗАДАНИЕ на научное исследование",
        f"**Наименование темы:** {topic if topic else 'устанавливает заказчик'}",
    ]
    tasks = _bullet_block(_tasks_lines(digest))
    criteria = _criteria_lines(digest)
    criteria_block = (
        "Критерии приёмки (из графа):\n" + _bullet_block(criteria)
        if criteria
        else f"Критерии приёмки: {_MISSING}"
    )
    composition = _bullet_block(_composition_lines(digest))
    constraints = _constraints_lines(digest)
    sections = [
        f"## 1 {PROSE_SECTIONS[0][1]}\n\n{prose.text_for('1')}",
        f"## 2 {PROSE_SECTIONS[1][1]}\n\n{prose.text_for('2')}",
        "## 2.1 Задачи исследования (из графа, дословно)\n\n" + tasks,
        "## 3 Требования к результату и критерии приёмки\n\n" + criteria_block,
        f"## 4 {PROSE_SECTIONS[2][1]}\n\n{prose.text_for('4')}",
        "## 5 Состав работ (из графа)\n\n" + composition,
        f"## 6 {PROSE_SECTIONS[3][1]}\n\n{prose.text_for('6')}",
    ]
    if constraints:
        sections.append("## 7 Особые требования\n\n" + _bullet_block(constraints))
    return "\n\n".join(header + sections) + "\n"


__all__ = ["PROSE_SECTIONS", "TzProse", "TzSection", "build_tz_markdown"]
