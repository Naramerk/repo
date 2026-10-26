"""NIR (НИР, ГОСТ 7.32-2017) — markdown framing + report-lane document builder.

``wrap_markdown_nir`` keeps the R16a.3 framing over the core study-report MD.
The report lane adds a full ГОСТ 7.32-2017 document built the legacy way
(``CoScientist/reporting/nir`` reference): STRUCTURE by code (title page,
содержание, section skeleton, источники, приложения — with ``<...>``
placeholders for organisation requisites the graph does not own), PROSE by
the model (``NirProse``), numbers only from the graph digest.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from coscientist.reporting.graph_digest import GraphDigest

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)

_MISSING = "не получено"

_HYPOTHESIS_KEY_WORDS = (
    "pervaya",
    "vtoraya",
    "tretya",
    "chetvertaya",
    "pyataya",
    "shestaya",
    "sedmaya",
    "vosmaya",
    "devyataya",
    "desyataya",
)

_METHODIKA_KEY = "metodika"
_RESULTS_KEY = "rezultaty"

_PLACEHOLDER = "<...>"


def wrap_markdown_nir(*, title: str, markdown: str) -> str:
    """Frame core ``render_markdown_report`` body with a config title only.

    No section/narrative logic — title comes from render config (not hardcoded).
    """
    return f"# {title}\n\n{markdown}"


class NirOutlineSection(BaseModel):
    """Code-planned body section: stable id + code hint title + digest facts."""

    model_config = _FROZEN

    id: StrictStr
    title_hint: StrictStr
    digest_facts: tuple[StrictStr, ...] = Field(default_factory=tuple)


class NirOutline(BaseModel):
    model_config = _FROZEN

    question: StrictStr
    sections: tuple[NirOutlineSection, ...]

    def section_ids(self) -> tuple[str, ...]:
        return tuple(section.id for section in self.sections)


class NirProse(BaseModel):
    """Model output for the НИР document (prose only; structure is code's)."""

    model_config = _FROZEN

    research_title: StrictStr = Field(min_length=1)
    abstract_text: StrictStr = Field(min_length=1)
    keywords: tuple[StrictStr, ...] = Field(min_length=5, max_length=15)
    introduction_paragraphs: tuple[StrictStr, ...] = Field(min_length=1)
    section_texts: dict[StrictStr, tuple[StrictStr, ...]] = Field(min_length=1)
    section_titles: dict[StrictStr, StrictStr] = Field(min_length=1)
    conclusion_paragraphs: tuple[StrictStr, ...] = Field(min_length=1)

    def prose_texts(self) -> tuple[str, ...]:
        chunks: list[str] = [self.research_title, self.abstract_text]
        chunks.extend(self.keywords)
        chunks.extend(self.introduction_paragraphs)
        chunks.extend(self.section_titles.values())
        for paragraphs in self.section_texts.values():
            chunks.extend(paragraphs)
        chunks.extend(self.conclusion_paragraphs)
        return tuple(chunks)


def _hypothesis_key(index: int) -> str:
    if 1 <= index <= len(_HYPOTHESIS_KEY_WORDS):
        return f"proverka-gipotezy-{_HYPOTHESIS_KEY_WORDS[index - 1]}"
    return f"proverka-gipotezy-{index}"


def _section_title(prose: NirProse, section: NirOutlineSection) -> str:
    title = prose.section_titles.get(section.id)
    if title is None:
        return section.title_hint
    return title


def _section_paragraphs(prose: NirProse, section: NirOutlineSection) -> tuple[str, ...]:
    paragraphs = prose.section_texts.get(section.id)
    if paragraphs is None:
        return ()
    return paragraphs


def build_nir_outline(digest: GraphDigest) -> NirOutline:
    """Plan the НИР body: методика, per-hypothesis sections, обобщённые результаты."""
    question = ""
    for label in digest.question_labels():
        for fact in digest.facts_for(label):
            if fact.startswith("Формулировка: "):
                question = fact.removeprefix("Формулировка: ")
                break
        if question:
            break
    sections = [
        NirOutlineSection(
            id=_METHODIKA_KEY,
            title_hint="Методика исследования и использованные средства",
            digest_facts=_methodika_facts(digest),
        )
    ]
    for index, label in enumerate(digest.hypothesis_labels(), start=1):
        sections.append(
            NirOutlineSection(
                id=_hypothesis_key(index),
                title_hint=f"Проверка гипотезы: {label.removeprefix('Гипотеза ')}",
                digest_facts=digest.facts_for(label),
            )
        )
    sections.append(
        NirOutlineSection(
            id=_RESULTS_KEY,
            title_hint="Обобщённые результаты и их обсуждение",
            digest_facts=_conclusion_facts(digest),
        )
    )
    outline_question = question
    if not outline_question:
        outline_question = _MISSING
    return NirOutline(question=outline_question, sections=tuple(sections))


def _methodika_facts(digest: GraphDigest) -> tuple[str, ...]:
    facts: list[str] = []
    for label in digest.experiment_labels():
        body = "; ".join(digest.facts_for(label))
        if body:
            facts.append(f"{label}: {body}")
    return tuple(facts)


def _conclusion_facts(digest: GraphDigest) -> tuple[str, ...]:
    facts: list[str] = []
    for label, lines in digest.facts_by_label.items():
        if not label.startswith("Вывод "):
            continue
        facts.extend(lines)
    if not facts:
        for label, lines in digest.facts_by_label.items():
            if label.startswith("Утверждение "):
                facts.extend(lines)
    return tuple(facts)


def _title_page(prose: NirProse) -> list[str]:
    return [
        "# ОТЧЁТ О НАУЧНО-ИССЛЕДОВАТЕЛЬСКОЙ РАБОТЕ",
        f"**{prose.research_title.strip()}**",
        f"(утверждает: {_PLACEHOLDER}; УДК: {_PLACEHOLDER}; "
        f"№ гос. регистрации: {_PLACEHOLDER})",
    ]


def _references_block(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label in digest.artifact_labels():
        doi = ""
        title = ""
        for fact in digest.facts_for(label):
            if fact.startswith("DOI или URL: "):
                doi = fact.removeprefix("DOI или URL: ")
            elif fact.startswith("Название: "):
                title = fact.removeprefix("Название: ")
        if doi or title:
            entry = " // ".join(part for part in (title, doi) if part)
            lines.append(f"- {entry}")
    return lines


def _appendix_block(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label in digest.artifact_labels():
        body = "; ".join(digest.facts_for(label))
        lines.append(f"- {label} — {body}" if body else f"- {label} — {_MISSING}")
    return lines


def build_nir_markdown(digest: GraphDigest, outline: NirOutline, prose: NirProse) -> str:
    """Assemble the НИР markdown (code structure + model prose)."""
    parts = _title_page(prose)
    parts.append(f"**Исполнители:** {_PLACEHOLDER} (в графе исследования нет данных об исполнителях)")
    parts.append(
        "## Реферат\n\n"
        + prose.abstract_text.strip()
        + "\n\n**Ключевые слова:** "
        + "; ".join(word.strip() for word in prose.keywords)
    )
    headings = [
        ("Введение", None),
        *[(_section_title(prose, s), i + 1) for i, s in enumerate(outline.sections)],
        ("Заключение", None),
        ("Список использованных источников", None),
        ("Приложения", None),
    ]
    toc: list[str] = []
    for title, number in headings:
        if number:
            toc.append(f"{number}. {title}")
        else:
            toc.append(title)
    parts.append("## Содержание\n\n" + "\n".join(f"- {line}" for line in toc))
    parts.append("## Введение\n\n" + "\n\n".join(p.strip() for p in prose.introduction_paragraphs))
    for index, section in enumerate(outline.sections, start=1):
        paragraphs = _section_paragraphs(prose, section)
        body = _MISSING
        if paragraphs:
            body = "\n\n".join(p.strip() for p in paragraphs)
        parts.append(f"## {index} {_section_title(prose, section)}\n\n{body}")
    parts.append("## Заключение\n\n" + "\n\n".join(p.strip() for p in prose.conclusion_paragraphs))
    references = _references_block(digest)
    if references:
        parts.append("## Список использованных источников\n\n" + "\n".join(references))
    else:
        parts.append(f"## Список использованных источников\n\n{_MISSING}")
    appendix = _appendix_block(digest)
    if appendix:
        parts.append("## Приложения\n\n" + "\n".join(appendix))
    return "\n\n".join(parts) + "\n"


__all__ = [
    "NirOutline",
    "NirOutlineSection",
    "NirProse",
    "build_nir_markdown",
    "build_nir_outline",
    "wrap_markdown_nir",
]
