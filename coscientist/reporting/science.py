"""Scientific report for researchers — prose slots filled by the model.

Structure is code-built (fixed Russian headings); the model only fills the
seven prose slots (:class:`ScienceReportProse`). Numbers in the prose must
trace to the graph digest; the honesty gate in ``coscientist.reporting.honesty``
validates each slot before assembly. The materials section is appended by
code from the digest's artifact facts (reproducibility refs stay exact).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from coscientist.reporting.graph_digest import GraphDigest

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)

_MISSING = "не получено"


class ScienceReportProse(BaseModel):
    """Model-filled slots of the scientific report (no structure control)."""

    model_config = _FROZEN

    aim: StrictStr = Field(min_length=1)
    approach: StrictStr = Field(min_length=1)
    results: StrictStr = Field(min_length=1)
    discussion: StrictStr = Field(min_length=1)
    limitations: StrictStr = Field(min_length=1)
    next_step: StrictStr = Field(min_length=1)
    materials: StrictStr = Field(min_length=1)

    def slots(self) -> tuple[tuple[str, str], ...]:
        return (
            ("aim", self.aim),
            ("approach", self.approach),
            ("results", self.results),
            ("discussion", self.discussion),
            ("limitations", self.limitations),
            ("next_step", self.next_step),
            ("materials", self.materials),
        )


def _artifact_appendix(digest: GraphDigest) -> list[str]:
    lines: list[str] = []
    for label in digest.artifact_labels():
        facts = digest.facts_by_label.get(label, ())
        body = "; ".join(facts)
        lines.append(f"- {label} — {body}" if body else f"- {label} — {_MISSING}")
    return lines


def build_science_report_markdown(digest: GraphDigest, prose: ScienceReportProse) -> str:
    """Assemble the final scientific report markdown (code structure + prose)."""
    sections = [
        "# Научный отчёт по исследованию",
        "## Цель",
        prose.aim.strip(),
        "## Подход",
        prose.approach.strip(),
        "## Результаты",
        prose.results.strip(),
        "## Обсуждение",
        prose.discussion.strip(),
        "## Ограничения",
        prose.limitations.strip(),
        "## Следующий шаг",
        prose.next_step.strip(),
        "## Материалы и воспроизводимость",
        prose.materials.strip(),
    ]
    appendix = _artifact_appendix(digest)
    if appendix:
        sections.append("Артефакты исследования (по графу):")
        sections.extend(appendix)
    return "\n\n".join(sections) + "\n"


__all__ = ["ScienceReportProse", "build_science_report_markdown"]
