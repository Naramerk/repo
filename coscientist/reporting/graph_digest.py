"""Compact research-graph digest — the single source for report documents.

Builds, in code, from the journal projections (``graph_nodes`` /
``graph_edges`` rows — never the raw transcript), a compact Russian digest of
the scientific substance: study goal, questions, hypotheses (+verdict),
experiments (+attempts), evidence, artifacts, claims, conclusions, criteria
and the edges between them. Every number rendered here is part of the
digest facts the honesty gate checks generated prose against, so what the
digest shows is exactly what documents may state.

Internal node ids never appear: entities are labelled ordinally
(«Гипотеза 1», «Наблюдение 2»). Missing data renders as «не получено».
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from coscientist.reporting.honesty import extract_number_tokens

if TYPE_CHECKING:
    from coscientist.reporting.honesty import DigestFacts

# Kinds rendered in the digest (scientific substance only; tool_call /
# mcp_exchange / hitl / plan / report machinery stays out).
_DIGEST_KINDS = (
    "study",
    "question",
    "hypothesis",
    "experiment",
    "evidence",
    "artifact",
    "claim",
    "conclusion",
    "criterion",
    "constraint",
)

_KIND_LABEL: dict[str, str] = {
    "study": "Исследование",
    "question": "Вопрос",
    "hypothesis": "Гипотеза",
    "experiment": "Эксперимент",
    "evidence": "Наблюдение",
    "artifact": "Артефакт",
    "claim": "Утверждение",
    "conclusion": "Вывод",
    "criterion": "Критерий",
    "constraint": "Ограничение",
}

_KIND_PLURAL_RU: dict[str, str] = {
    "question": "Вопросы",
    "hypothesis": "Гипотезы",
    "experiment": "Эксперименты",
    "evidence": "Наблюдения",
    "artifact": "Артефакты",
    "claim": "Утверждения",
    "conclusion": "Выводы",
    "criterion": "Критерии",
    "constraint": "Ограничения",
}

_KIND_TOTAL_RU: dict[str, str] = {
    "question": "вопросов",
    "hypothesis": "гипотез",
    "experiment": "экспериментов",
    "evidence": "наблюдений",
    "artifact": "артефактов",
    "claim": "утверждений",
    "conclusion": "выводов",
    "criterion": "критериев",
    "constraint": "ограничений",
}

_HYP_VERDICT_RU = {
    "supported": "подтверждена",
    "survived": "выдержала проверку (попытка опровержения не удалась)",
    "refuted": "опровергнута",
    "inconclusive": "не разрешена имеющимися данными",
    "untested": "не проверялась",
}

_CLAIM_STANCE_RU = {
    "supports": "поддерживает",
    "refutes": "опровергает",
    "inconclusive": "не позволяет сделать вывод",
}

_CRITERION_STATUS_RU = {
    "met": "условие выполнено",
    "not_met": "условие не выполнено",
    "unassessed": "не проверено",
}

_EVIDENCE_KIND_RU = {
    "literature": "литературное",
    "computational": "расчётное",
    "experimental": "экспериментальное",
    "expert": "экспертное",
    "derivation": "вывод из первых принципов",
}

_EXPERIMENT_METHOD_RU = {
    "computational": "расчётный",
    "statistical": "статистический",
    "laboratory": "лабораторный",
    "literature_search": "обзор литературы",
    "expert": "экспертный",
    "simulation": "имитационное моделирование",
}

_ATTEMPT_OUTCOME_RU = {
    "SUCCESS": "успех",
    "FAILED": "неудача",
    "PARTIAL": "частично",
    "SKIPPED": "пропущена",
}

_ARTIFACT_ROLE_RU = {
    "paper": "статья",
    "data": "данные",
    "code": "код",
    "figure": "иллюстрация",
    "table": "таблица",
    "log": "рабочий журнал",
    "report": "отчёт",
}

_OP_RU = {"<": "<", "<=": "≤", "==": "=", ">=": "≥", ">": ">", "in": "∈"}

_MISSING = "не получено"

# Edge kinds rendered as relation lines (both endpoints must be digest kinds):
# kind → (verb, grammatical case of the destination label).
_EDGE_TEMPLATE = {
    "addresses": ("относится к", "dat"),
    "tests": ("проверяет", "acc"),
    "cites": ("опирается на", "acc"),
    "about": ("относится к", "dat"),
    "produced": ("создал", "acc"),
    "derived_from": ("получено из", "gen"),
    "uses_input": ("использует как вход", "acc"),
    "answers": ("отвечает на", "acc"),
    "based_on": ("опирается на", "acc"),
    "measured_by": ("оценивается", "instr"),
    "contradicts": ("противоречит", "dat"),
}

# Kind-word declension for the object position (ordinals stay as-is:
# «Гипотезу 1» reads naturally). Missing entries keep the nominative.
_CASES: dict[str, dict[str, str]] = {
    "acc": {"Гипотеза": "Гипотезу"},
    "dat": {"Вопрос": "Вопросу", "Гипотеза": "Гипотезе", "Утверждение": "Утверждению"},
    "gen": {"Артефакт": "Артефакта"},
    "instr": {"Критерий": "Критерием"},
}

_MAX_TEXT_CHARS = 600

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


class DigestRefuse(Exception):
    """Projection rows malformed for digest building (typed, fail-closed)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


# Row models read journal projections: unknown future columns are ignored
# (strict on the fields the digest uses — node_id/kind/study_id/attrs).
_ROW_CONFIG = ConfigDict(frozen=True, strict=True)


class _NodeRow(BaseModel):
    model_config = _ROW_CONFIG

    node_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    study_id: StrictStr = Field(min_length=1)
    attrs: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr | None = None


class _EdgeRow(BaseModel):
    model_config = _ROW_CONFIG

    edge_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    study_id: StrictStr = Field(min_length=1)
    src_id: StrictStr = Field(min_length=1)
    dst_id: StrictStr = Field(min_length=1)
    attrs: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr | None = None


class GraphDigest(BaseModel):
    """Rendered digest + label map (built by code from projections only)."""

    model_config = _FROZEN

    study_id: StrictStr
    markdown: StrictStr
    labels: dict[str, StrictStr]
    facts_by_label: dict[str, tuple[StrictStr, ...]]

    def facts(self) -> DigestFacts:
        from coscientist.reporting.honesty import DigestFacts

        return DigestFacts(self.markdown)

    def number_tokens(self) -> frozenset[str]:
        return extract_number_tokens(self.markdown)

    def question_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self.labels.values() if label.startswith("Вопрос "))

    def hypothesis_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self.labels.values() if label.startswith("Гипотеза "))

    def experiment_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self.labels.values() if label.startswith("Эксперимент "))

    def artifact_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self.labels.values() if label.startswith("Артефакт "))


def _clip(text: object) -> str:
    """Single-line, length-capped rendering of an attr value."""
    raw = " ".join(str(text).split()) if text is not None else ""
    if len(raw) > _MAX_TEXT_CHARS:
        return raw[:_MAX_TEXT_CHARS] + "…"
    return raw


def _attr(node: _NodeRow, name: str) -> str:
    return _clip(node.attrs.get(name))


def _word(mapping: Mapping[str, str], key: object, fallback: str = "") -> str:
    if isinstance(key, str) and key in mapping:
        return mapping[key]
    if key is None:
        return ""
    return fallback if fallback else _clip(key)


def _number(value: object) -> str:
    """Render a graph number exactly as the digest (and the facts) show it."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return _clip(value)


def _parse_node(row: Mapping[str, Any]) -> _NodeRow:
    try:
        return _NodeRow.model_validate(dict(row))
    except ValidationError as exc:
        raise DigestRefuse(f"graph_nodes row invalid: {exc}") from exc


def _parse_edge(row: Mapping[str, Any]) -> _EdgeRow:
    try:
        return _EdgeRow.model_validate(dict(row))
    except ValidationError as exc:
        raise DigestRefuse(f"graph_edges row invalid: {exc}") from exc


def _study_lines(node: _NodeRow) -> list[str]:
    goal = node.attrs.get("goal")
    goal_line = _clip(goal.get("statement")) if isinstance(goal, Mapping) else ""
    lines = [f"Цель: {goal_line or _MISSING}"]
    request = _attr(node, "request")
    if request:
        lines.append(f"Исходный запрос: {request}")
    constraints = node.attrs.get("constraints")
    if isinstance(constraints, Sequence) and not isinstance(constraints, (str, bytes)):
        for item in constraints:
            content = item.get("content") if isinstance(item, Mapping) else None
            if content:
                lines.append(f"Ограничение: {_clip(content)}")
    return lines


def _question_lines(node: _NodeRow) -> list[str]:
    lines = [f"Формулировка: {_attr(node, 'formulation') or _MISSING}"]
    gap = _attr(node, "gap")
    if gap:
        lines.append(f"Пробел в знаниях: {gap}")
    return lines


def _hypothesis_lines(node: _NodeRow) -> list[str]:
    lines = [f"Утверждение: {_attr(node, 'statement') or _MISSING}"]
    prediction = _attr(node, "prediction")
    if prediction:
        lines.append(f"Проверяемый прогноз: {prediction}")
    verdict = _word(_HYP_VERDICT_RU, node.attrs.get("verdict"))
    lines.append(f"Статус: {verdict or _MISSING}")
    return lines


def _experiment_lines(node: _NodeRow) -> list[str]:
    lines = [f"Название: {_attr(node, 'title') or _MISSING}"]
    goal = _attr(node, "goal")
    if goal:
        lines.append(f"Цель задачи: {goal}")
    method = _word(_EXPERIMENT_METHOD_RU, node.attrs.get("method"))
    if method:
        lines.append(f"Метод: {method}")
    attempts = node.attrs.get("attempts")
    if isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
        outcomes = []
        for a in attempts:
            if isinstance(a, Mapping) and a.get("outcome") is not None:
                outcomes.append(_word(_ATTEMPT_OUTCOME_RU, a.get("outcome")))
        summary = f"Попыток выполнено: {len(attempts)}"
        if outcomes:
            summary += "; исходы: " + ", ".join(outcomes)
        lines.append(summary)
    limitations = node.attrs.get("limitations")
    if isinstance(limitations, Sequence) and not isinstance(limitations, (str, bytes)):
        for item in limitations:
            text = item.get("text") if isinstance(item, Mapping) else None
            if text:
                lines.append(f"Ограничение метода: {_clip(text)}")
    return lines


def _evidence_lines(node: _NodeRow) -> list[str]:
    kind = _word(_EVIDENCE_KIND_RU, node.attrs.get("kind"))
    head = f"[{kind}] " if kind else ""
    lines = [f"{head}Наблюдение: {_attr(node, 'statement') or _MISSING}"]
    measured_on = _attr(node, "measured_on")
    if measured_on:
        lines.append(f"Измерено на: {measured_on}")
    return lines


def _artifact_lines(node: _NodeRow) -> list[str]:
    role = _word(_ARTIFACT_ROLE_RU, node.attrs.get("role"))
    title = _attr(node, "title")
    lines = [f"Название: {title or _MISSING}"]
    if role:
        lines.append(f"Тип: {role}")
    media_type = _attr(node, "media_type")
    if media_type:
        lines.append(f"Формат: {media_type}")
    doi = _attr(node, "doi_or_url")
    if doi:
        lines.append(f"DOI или URL: {doi}")
    version = _attr(node, "version")
    if version:
        lines.append(f"Версия: {version}")
    year = _number(node.attrs.get("year"))
    if year:
        lines.append(f"Год: {year}")
    return lines


def _claim_lines(node: _NodeRow) -> list[str]:
    stance = _word(_CLAIM_STANCE_RU, node.attrs.get("stance"))
    head = f"[{stance}]" if stance else ""
    reason = _attr(node, "reason")
    lines = [f"{head} Содержание: {reason or stance or _MISSING}"]
    status = _word(_CRITERION_STATUS_RU, node.attrs.get("criterion_status"))
    if status:
        lines.append(f"Проверка по критерию: {status}")
    numeric = (("p_value", "p-значение"), ("power", "мощность"), ("sesoi", "SESOI"))
    for name, label in numeric:
        value = _number(node.attrs.get(name))
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _conclusion_lines(node: _NodeRow) -> list[str]:
    lines = [f"Синтез: {_attr(node, 'synthesis') or _MISSING}"]
    how = _attr(node, "how_established")
    if how:
        lines.append(f"Как установлено: {how}")
    bounds = _attr(node, "validity_bounds")
    if bounds:
        lines.append(f"Границы применимости: {bounds}")
    return lines


def _criterion_lines(node: _NodeRow) -> list[str]:
    attrs = node.attrs
    metric = _clip(attrs.get("metric"))
    target = _clip(attrs.get("target"))
    op = _word(_OP_RU, attrs.get("op"))
    if metric:
        relation = " ".join(part for part in (metric, op, target) if part)
        lines = [f"Метрика и порог: {relation}"]
    else:
        lines = [f"Тип проверки: {_clip(attrs.get('check')) or _MISSING}"]
    numeric = (
        ("tolerance", "Допуск"),
        ("sesoi", "SESOI"),
        ("alpha", "уровень значимости α"),
        ("min_power", "минимальная мощность"),
    )
    for name, label in numeric:
        value = _number(attrs.get(name))
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _constraint_lines(node: _NodeRow) -> list[str]:
    subtype = _clip(node.attrs.get("subtype"))
    content = _attr(node, "content")
    head = f"[{subtype}] " if subtype else ""
    return [f"{head}{content or _MISSING}"]


_KIND_LINES = {
    "study": _study_lines,
    "question": _question_lines,
    "hypothesis": _hypothesis_lines,
    "experiment": _experiment_lines,
    "evidence": _evidence_lines,
    "artifact": _artifact_lines,
    "claim": _claim_lines,
    "conclusion": _conclusion_lines,
    "criterion": _criterion_lines,
    "constraint": _constraint_lines,
}


def _summary_lines(nodes: Sequence[_NodeRow]) -> list[str]:
    by_kind: dict[str, int] = {}
    for node in nodes:
        if node.kind != "study":
            by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
    lines = [
        f"Всего {_KIND_TOTAL_RU[kind]}: {by_kind[kind]}"
        for kind in _DIGEST_KINDS
        if kind != "study" and kind in by_kind
    ]
    verdicts = [
        str(n.attrs.get("verdict"))
        for n in nodes
        if n.kind == "hypothesis" and n.attrs.get("verdict") is not None
    ]
    for verdict, word in _HYP_VERDICT_RU.items():
        count = verdicts.count(verdict)
        if count:
            lines.append(f"Гипотез со статусом «{word}»: {count}")
    attempts_total = 0
    for node in nodes:
        if node.kind != "experiment":
            continue
        attempts = node.attrs.get("attempts")
        if isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
            attempts_total += len(attempts)
    if attempts_total:
        lines.append(f"Всего попыток выполнения задач: {attempts_total}")
    return lines


def _decline(label: str, case: str) -> str:
    parts = label.split(" ", 1)
    if len(parts) != 2:
        return label
    word, ordinal = parts
    declined = _CASES.get(case, {}).get(word, word)
    return f"{declined} {ordinal}"


def _edge_lines(edges: Sequence[_EdgeRow], labels: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for edge in sorted(edges, key=lambda e: (e.created_at or "", e.edge_id)):
        template = _EDGE_TEMPLATE.get(edge.kind)
        src = labels.get(edge.src_id)
        dst = labels.get(edge.dst_id)
        if template is None or src is None or dst is None:
            continue
        verb, case = template
        lines.append(f"{src} {verb} {_decline(dst, case)}.")
    return lines


def build_graph_digest(
    study_id: str,
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> GraphDigest:
    """Projections → compact digest. Malformed rows → :class:`DigestRefuse`."""
    parsed_nodes: list[_NodeRow] = []
    for row in nodes:
        node = _parse_node(row)
        if node.study_id != study_id:
            raise DigestRefuse(
                f"graph_nodes row {node.node_id!r} study_id {node.study_id!r} != {study_id!r}"
            )
        if node.kind in _KIND_LABEL:
            parsed_nodes.append(node)
    parsed_edges: list[_EdgeRow] = []
    for row in edges:
        edge = _parse_edge(row)
        if edge.study_id != study_id:
            raise DigestRefuse(
                f"graph_edges row {edge.edge_id!r} study_id {edge.study_id!r} != {study_id!r}"
            )
        parsed_edges.append(edge)

    labels: dict[str, str] = {}
    facts_by_label: dict[str, tuple[str, ...]] = {}
    counters: dict[str, int] = {}
    ordered = sorted(parsed_nodes, key=lambda n: (n.created_at or "", n.node_id))
    for node in ordered:
        counters[node.kind] = counters.get(node.kind, 0) + 1
        label = f"{_KIND_LABEL[node.kind]} {counters[node.kind]}"
        labels[node.node_id] = label
        facts_by_label[label] = tuple(
            StrictStr(line) for line in _KIND_LINES[node.kind](node)
        )

    sections: list[str] = ["# Дайджест исследования (по графу)"]
    sections.append("## Исследование")
    study_nodes = [n for n in ordered if n.kind == "study"]
    if study_nodes:
        sections.extend(f"- {line}" for line in _study_lines(study_nodes[0]))
    else:
        sections.append(f"- Цель: {_MISSING}")
    sections.append("## Сводка (числа из графа)")
    summary = _summary_lines(ordered)
    if summary:
        sections.extend(f"- {line}" for line in summary)
    else:
        sections.append(f"- {_MISSING}")
    for kind in _DIGEST_KINDS:
        kind_nodes = [n for n in ordered if n.kind == kind]
        if kind == "study" or not kind_nodes:
            continue
        sections.append(f"## {_KIND_PLURAL_RU[kind]}")
        for node in kind_nodes:
            label = labels[node.node_id]
            kind_lines = _KIND_LINES[kind](node)
            sections.append(f"- {label}: {kind_lines[0]}")
            sections.extend(f"  {line}" for line in kind_lines[1:])
    relations = _edge_lines(parsed_edges, labels)
    if relations:
        sections.append("## Связи")
        sections.extend(f"- {line}" for line in relations)
    return GraphDigest(
        study_id=study_id,
        markdown="\n".join(sections) + "\n",
        labels=labels,
        facts_by_label=facts_by_label,
    )


__all__ = ["DigestRefuse", "GraphDigest", "build_graph_digest"]
