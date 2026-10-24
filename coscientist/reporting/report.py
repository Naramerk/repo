"""Study report formats json/md/html over core MinimalReport (R16a.1)."""

from __future__ import annotations

import json
from typing import Literal, assert_never
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationInfo,
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
    """Required render config — no silent prefix/lang default."""

    model_config = _FROZEN
    artifact_link_prefix: StrictStr = Field(min_length=1)
    html_lang: StrictStr = Field(min_length=1)

    @field_validator("artifact_link_prefix", "html_lang")
    @classmethod
    def _stripped_no_newlines(cls, v: str, info: ValidationInfo) -> str:
        if v != v.strip() or "\n" in v or "\r" in v:
            raise ValueError(f"{info.field_name} must be stripped, no newlines")
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


class ReportView(BaseModel):
    """Rendered report DTO — media_type is not stored; use ``media_type_for``."""

    model_config = _FROZEN
    study_id: StrictStr
    format: ReportFormat
    body: StrictStr


def media_type_for(fmt: ReportFormat) -> str:
    """Media type for a report format (single source with ``_MEDIA``)."""
    return _MEDIA[fmt]


def parse_report_format(raw: str) -> ReportFormat:
    """Transport str → ReportFormat; unknown → ReportFormatRefuse."""
    if raw == "json":
        return "json"
    if raw == "md":
        return "md"
    if raw == "html":
        return "html"
    raise ReportFormatRefuse(f"unsupported report format: {raw!r}", format=raw)


def artifact_href(aid: str, config: ReportFormatConfig) -> str:
    """Shared MD/HTML artifact URL: prefix + percent-encoded id."""
    return f"{config.artifact_link_prefix}{quote(aid, safe='')}"


def _artifacts_appendix(aid: str | None, config: ReportFormatConfig) -> str:
    if aid is None:
        return "## Artifacts\n_None._\n"
    href = artifact_href(aid, config)
    # Code-span label: aid as-is (no backslash escapes). Edge case → #119.
    return f"## Artifacts\n- [`{aid}`]({href})\n"


def render_report_body(
    inp: ReportRenderInput,
    fmt: ReportFormat,
    config: ReportFormatConfig,
) -> str:
    """Render report body only; callers use ``media_type_for(fmt)`` for media."""
    md_core = render_markdown_report(inp.report)
    md = f"{md_core}{_artifacts_appendix(inp.report_artifact_id, config)}"
    if fmt == "json":
        return (
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
    if fmt == "md":
        return md
    if fmt == "html":
        artifact: tuple[str, str] | None = (
            None
            if inp.report_artifact_id is None
            else (
                inp.report_artifact_id,
                artifact_href(inp.report_artifact_id, config),
            )
        )
        return wrap_markdown_html(
            study_id=inp.report.study_id,
            markdown=md,
            artifact=artifact,
            lang=config.html_lang,
        )
    assert_never(fmt)


def get_study_report(
    inp: ReportRenderInput,
    *,
    format: ReportFormat,
    config: ReportFormatConfig,
) -> ReportView:
    return ReportView(
        study_id=inp.report.study_id,
        format=format,
        body=render_report_body(inp, format, config),
    )
