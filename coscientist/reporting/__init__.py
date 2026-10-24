"""Reporting projections (M5 / CAPABILITIES #16)."""

from coscientist.reporting.render_html import wrap_markdown_html
from coscientist.reporting.report import (
    ReportFormat,
    ReportFormatConfig,
    ReportFormatRefuse,
    ReportingError,
    ReportRenderInput,
    ReportView,
    get_study_report,
    render_report,
)

__all__ = [
    "ReportFormat",
    "ReportFormatConfig",
    "ReportFormatRefuse",
    "ReportRenderInput",
    "ReportView",
    "ReportingError",
    "get_study_report",
    "render_report",
    "wrap_markdown_html",
]
