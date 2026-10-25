"""R16a.3: format=nir — core MD body + config title framing only."""

from __future__ import annotations

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
from coscientist.core.finalize.minimal_report import (
    assemble_minimal_report,
    render_markdown_report,
)
from coscientist.reporting import (
    ReportFormatConfig,
    ReportRenderInput,
    get_study_report,
    parse_report_format,
    render_report_body,
    wrap_markdown_nir,
)
from coscientist.reporting.report import media_type_for

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_CFG = ReportFormatConfig(
    artifact_link_prefix="artifact:",
    html_lang="en",
    nir_title="Отчёт о НИР",
)


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


def _input() -> ReportRenderInput:
    tasks = {"st/A": _task("A")}
    report = assemble_minimal_report(
        study_id="st_nir",
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
                "formulation": "q",
            }
        },
        fallback_reason=None,
        refusal=None,
        hypotheses={},
        hypotheses_unavailable=None,
        custom_checks=[],
        observations=[],
    )
    return ReportRenderInput(report=report, report_artifact_id="art_x", tasks=tasks)


def test_parse_nir() -> None:
    assert parse_report_format("nir") == "nir"
    assert media_type_for("nir") == "text/markdown; charset=utf-8"


def test_config_requires_nir_title() -> None:
    with pytest.raises(ValidationError, match="nir_title"):
        ReportFormatConfig.model_validate(
            {"artifact_link_prefix": "artifact:", "html_lang": "en"}
        )


def test_nir_title_stripped_no_newlines() -> None:
    base = {
        "artifact_link_prefix": "artifact:",
        "html_lang": "en",
        "nir_title": "t\n",
    }
    with pytest.raises(ValidationError, match="nir_title must be stripped"):
        ReportFormatConfig.model_validate(base)


def test_wrap_markdown_nir_framing_only() -> None:
    assert wrap_markdown_nir(title="T", markdown="# body\n") == "# T\n\n# body\n"


def test_format_nir_uses_core_md_plus_config_title() -> None:
    inp = _input()
    core = render_markdown_report(inp.report, tasks=inp.tasks)
    body = render_report_body(inp, "nir", _CFG)
    assert body == f"# {_CFG.nir_title}\n\n{core}"
    # No artifacts appendix / no second section renderer
    assert "## Artifacts" not in body
    assert body.startswith(f"# {_CFG.nir_title}\n\n")


def test_nir_title_from_config_not_hardcoded() -> None:
    inp = _input()
    cfg = ReportFormatConfig(
        artifact_link_prefix="artifact:",
        html_lang="en",
        nir_title="Custom NIR Title",
    )
    body = render_report_body(inp, "nir", cfg)
    assert body.startswith("# Custom NIR Title\n\n")
    assert "Отчёт о НИР" not in body


def test_get_study_report_nir() -> None:
    view = get_study_report(_input(), format="nir", config=_CFG)
    assert view.format == "nir"
    assert view.body == render_report_body(_input(), "nir", _CFG)
