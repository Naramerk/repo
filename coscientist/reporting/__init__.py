"""Reporting projections (M5 / CAPABILITIES #16)."""

from coscientist.reporting.render_html import wrap_markdown_html
from coscientist.reporting.report import (
    ReportFormat,
    ReportFormatConfig,
    ReportFormatRefuse,
    ReportingError,
    ReportRenderInput,
    ReportView,
    artifact_href,
    get_study_report,
    media_type_for,
    parse_report_format,
    render_report,
)

__all__ = [
    "ReportFormat",
    "ReportFormatConfig",
    "ReportFormatRefuse",
    "ReportRenderInput",
    "ReportView",
    "ReportingError",
    "artifact_href",
    "get_study_report",
    "media_type_for",
    "parse_report_format",
    "render_report",
    "wrap_markdown_html",
]
