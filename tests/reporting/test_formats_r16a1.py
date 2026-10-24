"""R16a.1: reporting formats json/md/html over core MinimalReport."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import (
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.finalize.minimal_report import assemble_minimal_report
from coscientist.reporting import (
    ReportFormatConfig,
    ReportFormatRefuse,
    ReportRenderInput,
    get_study_report,
    render_report,
)

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_Q = {"Q1": {"disposition": "not_attempted", "reason": "no_task", "formulation": "q"}}
_CFG = ReportFormatConfig(artifact_link_prefix="artifact:")


def _task(key: str) -> Task:
    return Task(
        task_id=f"st/{key}",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key=key, title=key, goal="g", route="experiment"),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        reason=None,
        deadline=_TS,
    )


def _input(*, aid: str | None = "art_deadbeef") -> ReportRenderInput:
    report = assemble_minimal_report(
        study_id="st_16a1",
        terminal_reason="completed",
        trigger="all_done",
        contributing_reasons=[],
        verdict="partial",
        tasks={"st/A": _task("A")},
        attempts={},
        question_dispositions=_Q,
        fallback_reason=None,
    )
    return ReportRenderInput(report=report, report_artifact_id=aid)


def test_config_requires_prefix():
    with pytest.raises(ValidationError, match="artifact_link_prefix"):
        ReportFormatConfig.model_validate({})


def test_format_md_appends_artifact_link():
    inp = _input()
    md, media = render_report(inp, "md", _CFG)
    assert media == "text/markdown; charset=utf-8"
    assert md.startswith("# Study report `st_16a1`\n")
    assert md.endswith("- [`art_deadbeef`](artifact:art_deadbeef)\n")


def test_format_md_none_artifact():
    inp = _input(aid=None)
    md, _ = render_report(inp, "md", _CFG)
    assert md.endswith("## Artifacts\n_None._\n")


def test_format_html_wraps_same_md():
    inp = _input()
    md, _ = render_report(inp, "md", _CFG)
    html_body, media = render_report(inp, "html", _CFG)
    assert media == "text/html; charset=utf-8"
    assert f'<p id="artifact-art_deadbeef"><a href="artifact:art_deadbeef">' in html_body
    assert f"<pre>{html.escape(md)}</pre>" in html_body
    assert "<title>st_16a1</title>" in html_body


def test_format_json_structured_dump():
    inp = _input()
    md, _ = render_report(inp, "md", _CFG)
    js, media = render_report(inp, "json", _CFG)
    assert media == "application/json; charset=utf-8"
    assert json.loads(js) == {
        "study_id": "st_16a1",
        "minimal_report": inp.report.model_dump(mode="json"),
        "report_artifact_id": "art_deadbeef",
        "markdown": md,
    }


def test_format_refuse_unknown():
    inp = _input()
    with pytest.raises(ReportFormatRefuse, match="unsupported report format") as ei:
        render_report(inp, "pdf", _CFG)  # type: ignore[arg-type]
    assert ei.value.format == "pdf"


def test_refuse_backtick_artifact_id():
    report = _input().report
    with pytest.raises(ValidationError, match="backtick"):
        ReportRenderInput(report=report, report_artifact_id="art_`x")


def test_api_view_matches_render():
    inp = _input()
    view = get_study_report(inp, format="md", config=_CFG)
    expected, _ = render_report(inp, "md", _CFG)
    assert view.body == expected
    assert view.format == "md"
    assert view.study_id == "st_16a1"
