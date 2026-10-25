"""Reporting projections (M5 / CAPABILITIES #16)."""

from coscientist.reporting.render_html import wrap_markdown_html
from coscientist.reporting.report import (
    ReportFormat,
    ReportFormatConfig,
    ReportFormatRefuse,
    ReportingError,
    ReportInputRefuse,
    ReportRenderInput,
    ReportView,
    artifact_href,
    get_study_report,
    media_type_for,
    parse_report_format,
    parse_report_input,
    render_report_body,
)

__all__ = [
    "ReportFormat",
    "ReportFormatConfig",
    "ReportFormatRefuse",
    "ReportInputRefuse",
    "ReportRenderInput",
    "ReportView",
    "ReportingError",
    "artifact_href",
    "get_study_report",
    "media_type_for",
    "parse_report_format",
    "parse_report_input",
    "render_report_body",
    "wrap_markdown_html",
]
