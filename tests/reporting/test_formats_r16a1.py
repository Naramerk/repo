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
    ReportView,
    artifact_href,
    get_study_report,
    parse_report_format,
    render_report_body,
)
from coscientist.reporting.render_html import wrap_markdown_html
from coscientist.reporting.report import media_type_for

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_CFG = ReportFormatConfig(artifact_link_prefix="artifact:", html_lang="en")
_CUSTOM_AID = "id with space&x"
_CUSTOM_HREF = "cas://blob/id%20with%20space%26x"


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
    tasks = {"st/A": _task("A")}
    report = assemble_minimal_report(
        study_id=study_id,
        terminal_reason="completed",
        trigger="all_done",
        contributing_reasons=[],
        verdict="partial",
        tasks=tasks,
        attempts={},
        question_dispositions={
            "Q1": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": formulation,
            }
        },
        fallback_reason=None,
        refusal=None,
        hypotheses={},
        hypotheses_unavailable=None,
        custom_checks=[],
        observations=[],
    )
    return ReportRenderInput(report=report, report_artifact_id=aid, tasks=tasks)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"html_lang": "en"}, "artifact_link_prefix"),
        ({"artifact_link_prefix": "artifact:"}, "html_lang"),
    ],
    ids=["missing-prefix", "missing-lang"],
)
def test_config_requires_prefix_and_lang(payload: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ReportFormatConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("html_lang", " en"),
        ("html_lang", "en "),
        ("html_lang", "en\n"),
        ("html_lang", "en\r"),
        ("artifact_link_prefix", " artifact:"),
        ("artifact_link_prefix", "artifact: "),
        ("artifact_link_prefix", "artifact:\n"),
        ("artifact_link_prefix", "artifact:\r"),
    ],
    ids=[
        "html_lang-leading-space",
        "html_lang-trailing-space",
        "html_lang-newline",
        "html_lang-cr",
        "prefix-leading-space",
        "prefix-trailing-space",
        "prefix-newline",
        "prefix-cr",
    ],
)
def test_config_stripped_no_newlines(field: str, value: str) -> None:
    base = {"artifact_link_prefix": "artifact:", "html_lang": "en"}
    base[field] = value
    with pytest.raises(ValidationError, match=f"{field} must be stripped"):
        ReportFormatConfig.model_validate(base)


def test_format_md_appends_artifact_link() -> None:
    inp = _input()
    md = render_report_body(inp, "md", _CFG)
    assert md.startswith("# Study report `st_16a1`\n")
    assert md.endswith("- [`art_deadbeef`](artifact:art_deadbeef)\n")


def test_aid_in_code_span_no_backslash_escape() -> None:
    """R16a.1a: MD code-span label is raw aid; URL stays percent-encoded (#119)."""
    aid = "art_[x](y)"
    md = render_report_body(_input(aid=aid), "md", _CFG)
    line = md.splitlines()[-1]
    assert line == f"- [`{aid}`]({artifact_href(aid, _CFG)})"
    assert "\\[" not in line and "\\]" not in line
    assert "\\(" not in line and "\\)" not in line
    assert "%5B" in line and "%5D" in line and "%28" in line and "%29" in line


def test_format_md_none_artifact() -> None:
    md = render_report_body(_input(aid=None), "md", _CFG)
    assert md.endswith("## Artifacts\n_None._\n")


def test_format_html_wraps_same_md() -> None:
    inp = _input()
    md = render_report_body(inp, "md", _CFG)
    html_body = render_report_body(inp, "html", _CFG)
    assert '<html lang="en">' in html_body
    assert '<p id="artifact-art_deadbeef"><a href="artifact:art_deadbeef">' in html_body
    assert f"<pre>{html.escape(md)}</pre>" in html_body
    assert "<title>st_16a1</title>" in html_body


def test_format_html_none_artifact() -> None:
    html_body = render_report_body(_input(aid=None), "html", _CFG)
    assert 'id="artifact-' not in html_body
    assert "<a href=" not in html_body
    assert "<pre>" in html_body
    assert "_None._" in html_body


def test_html_lang_from_config() -> None:
    cfg = ReportFormatConfig(artifact_link_prefix="artifact:", html_lang="de")
    html_body = render_report_body(_input(aid=None), "html", cfg)
    assert '<html lang="de">' in html_body


def test_html_lang_escaped() -> None:
    cfg = ReportFormatConfig(
        artifact_link_prefix="artifact:",
        html_lang='en"><script>x</script>',
    )
    html_body = render_report_body(_input(aid=None), "html", cfg)
    assert "<script>" not in html_body
    assert f'lang="{html.escape(cfg.html_lang, quote=True)}"' in html_body


def test_wrap_markdown_html_artifact_pair_none() -> None:
    out = wrap_markdown_html(
        study_id="st",
        markdown="# hi",
        artifact=None,
        lang="en",
    )
    assert "<a href=" not in out
    assert '<html lang="en">' in out


def test_wrap_markdown_html_artifact_pair() -> None:
    out = wrap_markdown_html(
        study_id="st",
        markdown="# hi",
        artifact=("aid1", "cas://blob/aid1"),
        lang="fr",
    )
    assert '<p id="artifact-aid1"><a href="cas://blob/aid1">aid1</a></p>' in out
    assert '<html lang="fr">' in out


def test_custom_prefix_href_literal() -> None:
    cfg = ReportFormatConfig(artifact_link_prefix="cas://blob/", html_lang="en")
    assert artifact_href(_CUSTOM_AID, cfg) == _CUSTOM_HREF


def test_custom_prefix_md_link() -> None:
    cfg = ReportFormatConfig(artifact_link_prefix="cas://blob/", html_lang="en")
    md = render_report_body(_input(aid=_CUSTOM_AID), "md", cfg)
    assert f"[`{_CUSTOM_AID}`]({_CUSTOM_HREF})" in md


def test_custom_prefix_html_href() -> None:
    cfg = ReportFormatConfig(artifact_link_prefix="cas://blob/", html_lang="en")
    html_body = render_report_body(_input(aid=_CUSTOM_AID), "html", cfg)
    assert f'href="{html.escape(_CUSTOM_HREF, quote=True)}"' in html_body
    assert f'id="artifact-{html.escape(_CUSTOM_AID, quote=True)}"' in html_body


def test_html_escapes_adversarial_payload() -> None:
    evil_sid = 'st<script>alert(1)</script>&"'
    evil_aid = "\"><script>x</script>'&"
    evil_q = "<img src=x onerror=alert(2)>"
    inp = _input(aid=evil_aid, study_id=evil_sid, formulation=evil_q)
    html_body = render_report_body(inp, "html", _CFG)
    assert "<script>" not in html_body
    assert "<img " not in html_body
    assert '"><script>' not in html_body
    assert f"<title>{html.escape(evil_sid, quote=True)}</title>" in html_body
    assert html.escape(evil_aid, quote=True) in html_body
    assert html.escape(evil_q) in html_body
    href = artifact_href(evil_aid, _CFG)
    assert f'href="{html.escape(href, quote=True)}"' in html_body


def test_format_json_structured_dump() -> None:
    inp = _input()
    md = render_report_body(inp, "md", _CFG)
    js = render_report_body(inp, "json", _CFG)
    assert json.loads(js) == {
        "study_id": "st_16a1",
        "minimal_report": inp.report.model_dump(mode="json"),
        "report_artifact_id": "art_deadbeef",
        "markdown": md,
    }


def test_parse_report_format_refuse_unknown() -> None:
    with pytest.raises(ReportFormatRefuse, match="unsupported report format") as ei:
        parse_report_format("pdf")
    assert ei.value.format == "pdf"


@pytest.mark.parametrize(
    "raw",
    ["json", "md", "html"],
    ids=["json", "md", "html"],
)
def test_parse_report_format_known(raw: str) -> None:
    assert parse_report_format(raw) == raw


def test_refuse_backtick_artifact_id() -> None:
    inp = _input()
    with pytest.raises(ValidationError, match="backtick"):
        ReportRenderInput(
            report=inp.report,
            report_artifact_id="art_`x",
            tasks=inp.tasks,
        )


def test_api_view_body_matches_render() -> None:
    inp = _input()
    view = get_study_report(inp, format="md", config=_CFG)
    assert view.body == render_report_body(inp, "md", _CFG)


def test_api_view_format_and_study_id() -> None:
    view = get_study_report(_input(), format="md", config=_CFG)
    assert view.format == "md"
    assert view.study_id == "st_16a1"


def test_api_view_media_via_format() -> None:
    view = get_study_report(_input(), format="md", config=_CFG)
    assert media_type_for(view.format) == "text/markdown; charset=utf-8"


def test_api_view_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"media_type[\s\S]*extra_forbidden"):
        ReportView.model_validate(
            {
                "study_id": "st",
                "format": "md",
                "body": "x",
                "media_type": "text/markdown; charset=utf-8",
            }
        )


@pytest.mark.parametrize(
    ("fmt", "media"),
    [
        ("json", "application/json; charset=utf-8"),
        ("md", "text/markdown; charset=utf-8"),
        ("html", "text/html; charset=utf-8"),
    ],
    ids=["json", "md", "html"],
)
def test_media_type_for_formats(fmt: str, media: str) -> None:
    assert media_type_for(parse_report_format(fmt)) == media
