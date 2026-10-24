"""R16a.1: reporting formats json/md/html over core MinimalReport."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from urllib.parse import quote

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
    ReportView,
    artifact_href,
    get_study_report,
    parse_report_format,
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


def _input(
    *,
    aid: str | None = "art_deadbeef",
    study_id: str = "st_16a1",
    formulation: str = "q",
) -> ReportRenderInput:
    report = assemble_minimal_report(
        study_id=study_id,
        terminal_reason="completed",
        trigger="all_done",
        contributing_reasons=[],
        verdict="partial",
        tasks={"st/A": _task("A")},
        attempts={},
        question_dispositions={
            "Q1": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": formulation,
            }
        },
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
    assert '<p id="artifact-art_deadbeef"><a href="artifact:art_deadbeef">' in html_body
    assert f"<pre>{html.escape(md)}</pre>" in html_body
    assert "<title>st_16a1</title>" in html_body


def test_format_html_none_artifact():
    inp = _input(aid=None)
    html_body, _ = render_report(inp, "html", _CFG)
    assert 'id="artifact-' not in html_body
    assert "<a href=" not in html_body
    assert "<pre>" in html_body
    assert "_None._" in html_body


def test_custom_prefix_md_and_html():
    cfg = ReportFormatConfig(artifact_link_prefix="cas://blob/")
    aid = "id with space&x"
    inp = _input(aid=aid)
    href = artifact_href(aid, cfg)
    assert href == f"cas://blob/{quote(aid, safe='')}"
    md, _ = render_report(inp, "md", cfg)
    assert f"[`{aid}`]({href})" in md
    html_body, _ = render_report(inp, "html", cfg)
    assert f'href="{html.escape(href, quote=True)}"' in html_body
    assert f'id="artifact-{html.escape(aid, quote=True)}"' in html_body


def test_html_escapes_adversarial_payload():
    evil_sid = 'st<script>alert(1)</script>&"'
    evil_aid = '"><script>x</script>\'&'
    evil_q = "<img src=x onerror=alert(2)>"
    inp = _input(aid=evil_aid, study_id=evil_sid, formulation=evil_q)
    html_body, _ = render_report(inp, "html", _CFG)
    # Raw markup must not appear as tags/attrs; escaped text may still contain
    # the substrings ``onerror=`` / ``script`` inside entities.
    assert "<script>" not in html_body
    assert "<img " not in html_body
    assert '"><script>' not in html_body
    assert f"<title>{html.escape(evil_sid, quote=True)}</title>" in html_body
    assert html.escape(evil_aid, quote=True) in html_body
    assert html.escape(evil_q) in html_body
    href = artifact_href(evil_aid, _CFG)
    assert f'href="{html.escape(href, quote=True)}"' in html_body


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


def test_parse_report_format_refuse_unknown():
    with pytest.raises(ReportFormatRefuse, match="unsupported report format") as ei:
        parse_report_format("pdf")
    assert ei.value.format == "pdf"


def test_parse_report_format_known():
    assert parse_report_format("json") == "json"
    assert parse_report_format("md") == "md"
    assert parse_report_format("html") == "html"


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
    assert view.media_type == "text/markdown; charset=utf-8"


def test_report_view_media_must_match_format():
    with pytest.raises(ValidationError, match="media_type"):
        ReportView(
            study_id="st",
            format="md",
            body="x",
            media_type="text/html; charset=utf-8",
        )
