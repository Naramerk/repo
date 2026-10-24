"""Study report formats json/md/html over core MinimalReport (R16a.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
)

from coscientist.core.finalize.minimal_report import render_markdown_report
from coscientist.core.finalize.minimal_report_models import MinimalReport
from coscientist.reporting.render_html import wrap_markdown_html

ReportFormat = Literal["json", "md", "html"]
_MEDIA: dict[ReportFormat, str] = {
    "json": "application/json; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}
_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReportingError(Exception):
    """Base reporting error."""


class ReportFormatRefuse(ReportingError):
    def __init__(self, detail: str, *, format: str) -> None:
        self.detail = detail
        self.format = format
        super().__init__(detail)


class ReportFormatConfig(BaseModel):
    """Required render config — no silent prefix default."""

    model_config = _FROZEN
    artifact_link_prefix: StrictStr = Field(min_length=1)

    @field_validator("artifact_link_prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        if v != v.strip() or "\n" in v or "\r" in v:
            raise ValueError("artifact_link_prefix must be stripped, no newlines")
        return v


class ReportRenderInput(BaseModel):
    """Payload-only input: typed report + optional CAS artifact id (no free markdown)."""

    model_config = _FROZEN
    report: MinimalReport
    report_artifact_id: StrictStr | None

    @field_validator("report_artifact_id")
    @classmethod
    def _aid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if "`" in v or "\n" in v or "\r" in v:
            raise ValueError("report_artifact_id must not contain backtick/newline")
        return v


@dataclass(frozen=True, slots=True)
class ReportView:
    study_id: str
    format: ReportFormat
    body: str
    media_type: str


def _artifacts_appendix(aid: str | None, config: ReportFormatConfig) -> str:
    if aid is None:
        return "## Artifacts\n_None._\n"
    href = f"{config.artifact_link_prefix}{quote(aid, safe='')}"
    # Code-span label: aid as-is (no backslash escapes). R16a.1a locks the edge case.
    return f"## Artifacts\n- [`{aid}`]({href})\n"


def render_report(
    inp: ReportRenderInput, fmt: ReportFormat, config: ReportFormatConfig
) -> tuple[str, str]:
    if fmt not in _MEDIA:
        raise ReportFormatRefuse(
            f"unsupported report format: {fmt!r}", format=str(fmt)
        )
    md_core = render_markdown_report(inp.report)
    md = f"{md_core}{_artifacts_appendix(inp.report_artifact_id, config)}"
    if fmt == "json":
        body = (
            json.dumps(
                {
                    "study_id": inp.report.study_id,
                    "minimal_report": inp.report.model_dump(mode="json"),
                    "report_artifact_id": inp.report_artifact_id,
                    "markdown": md,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    elif fmt == "md":
        body = md
    else:
        body = wrap_markdown_html(
            study_id=inp.report.study_id,
            markdown=md,
            report_artifact_id=inp.report_artifact_id,
        )
    return body, _MEDIA[fmt]


def get_study_report(
    inp: ReportRenderInput, *, format: ReportFormat, config: ReportFormatConfig
) -> ReportView:
    body, media = render_report(inp, format, config)
    return ReportView(
        study_id=inp.report.study_id,
        format=format,
        body=body,
        media_type=media,
    )
