"""R16a.2: one ReportInputRefuse path on construct and parse_report_input."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
    ReportInputRefuse,
    ReportRenderInput,
    parse_report_input,
)

_TS = datetime(2026, 1, 1, tzinfo=UTC)


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


def _valid_payload(*, aid: str | None = "art_ok") -> dict[str, Any]:
    tasks = {"st/A": _task("A")}
    report = assemble_minimal_report(
        study_id="st_16a2",
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
    return {
        "report": report,
        "report_artifact_id": aid,
        "tasks": tasks,
    }


@pytest.mark.parametrize(
    ("aid", "match"),
    [
        ("art_`x", "backtick"),
        ("art_\nx", "backtick/newline"),
        ("art_\rx", "backtick/newline"),
    ],
    ids=["backtick", "newline", "cr"],
)
def test_construct_refuse_bad_aid(aid: str, match: str) -> None:
    payload = _valid_payload()
    with pytest.raises(ReportInputRefuse, match=match):
        ReportRenderInput(
            report=payload["report"],
            report_artifact_id=aid,
            tasks=payload["tasks"],
        )


@pytest.mark.parametrize(
    ("aid", "match"),
    [
        ("art_`x", "backtick"),
        ("art_\nx", "backtick/newline"),
        ("art_\rx", "backtick/newline"),
    ],
    ids=["backtick", "newline", "cr"],
)
def test_parse_refuse_bad_aid(aid: str, match: str) -> None:
    payload = _valid_payload(aid=aid)
    with pytest.raises(ReportInputRefuse, match=match):
        parse_report_input(payload)


def test_parse_and_construct_same_refuse_type() -> None:
    payload = _valid_payload(aid="bad`id")
    with pytest.raises(ReportInputRefuse, match="backtick") as via_parse:
        parse_report_input(payload)
    with pytest.raises(ReportInputRefuse, match="backtick") as via_ctor:
        ReportRenderInput(**payload)
    assert type(via_parse.value) is type(via_ctor.value) is ReportInputRefuse


def test_parse_ok_none_artifact() -> None:
    inp = parse_report_input(_valid_payload(aid=None))
    assert inp.report_artifact_id is None


def test_parse_ok_study_id() -> None:
    inp = parse_report_input(_valid_payload(aid=None))
    assert inp.report.study_id == "st_16a2"


def test_parse_missing_tasks_refuse() -> None:
    payload = _valid_payload()
    del payload["tasks"]
    with pytest.raises(ReportInputRefuse, match="tasks"):
        parse_report_input(payload)


def test_parse_extra_forbidden() -> None:
    payload = _valid_payload()
    payload["markdown"] = "# free"
    with pytest.raises(ReportInputRefuse, match="extra_forbidden"):
        parse_report_input(payload)


def test_construct_does_not_leak_validation_error() -> None:
    payload = _valid_payload(aid="art_`x")
    try:
        ReportRenderInput(**payload)
    except ValidationError:
        pytest.fail("ValidationError leaked; expected ReportInputRefuse only")
    except ReportInputRefuse as e:
        assert "backtick" in str(e)
    else:
        pytest.fail("expected ReportInputRefuse")


def test_parse_does_not_leak_validation_error() -> None:
    payload = _valid_payload(aid="art_`x")
    try:
        parse_report_input(payload)
    except ValidationError:
        pytest.fail("ValidationError leaked; expected ReportInputRefuse only")
    except ReportInputRefuse as e:
        assert "backtick" in str(e)
    else:
        pytest.fail("expected ReportInputRefuse")
