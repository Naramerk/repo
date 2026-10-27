"""Issue #279: human-readable report at ANY terminal (universal-acceptance S2).

Non-fulfilled terminals (refused / failed / cancelled / partial) get a CAS
``report.md`` whose explanation is built from the typed journal fold only and
carries the legacy «no scientific acceptance is asserted» disclaimer
(quality_basis pattern). Fulfilled rendering stays as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coscientist.core.claims.reasons import RefusalKind, TaskReason
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import TaskOutcome
from coscientist.core.finalize.finalize import finalize as finalize_mod_fn
from coscientist.core.finalize.minimal_report import (
    NON_FULFILLED_DISCLAIMER,
    assemble_minimal_report,
    render_markdown_report,
)
from tests.helpers import close_runtime, open_runtime
from tests.test_finalize import _boot, _closed_attempts, _fin, _skip_all, _task


def _md_by_name(arts: LocalArtifactStore) -> dict[str, str]:
    """artifact name → markdown body (via meta JSON + CAS blob)."""
    out: dict[str, str] = {}
    for meta_file in sorted(arts.meta.glob("*.json")):
        row = json.loads(meta_file.read_text(encoding="utf-8"))
        out[str(row["name"])] = (arts.cas / str(row["sha256"])).read_text(encoding="utf-8")
    return out


def _disclaimer_report(verdict: str):
    tasks, attempts = _closed_attempts("A", provenance="real")
    tasks["st/F"] = _task("F", outcome=TaskOutcome.FAILED, reason=TaskReason.FAILED)
    return (
        assemble_minimal_report(
            study_id="st_any_term",
            terminal_reason="failed",
            trigger="all_done",
            contributing_reasons=["failed"],
            verdict=verdict,
            tasks=tasks,
            attempts=attempts,
            question_dispositions={
                "Q1": {"disposition": "not_attempted", "reason": "no_task", "formulation": "q"},
            },
            fallback_reason=None,
            refusal=None,
            hypotheses={},
            hypotheses_unavailable=None,
            custom_checks=[],
            observations=[],
        ),
        tasks,
    )


def test_non_fulfilled_report_has_disclaimer_and_typed_reasons():
    report, tasks = _disclaimer_report("failed")
    md = render_markdown_report(report, tasks=tasks)
    assert "## Honest outcome" in md
    assert NON_FULFILLED_DISCLAIMER in md
    assert "no scientific acceptance is asserted" in md
    # Typed journal facts, not prose: failed task and open question by name.
    assert "`F` [experiment] FAILED reason=failed" in md
    assert "задач не выполнено: 1" in md
    assert "`Q1` not_attempted (no_task)" in md
    assert "вопросов не закрыто: 1" in md


def test_non_fulfilled_report_carries_refusal_and_fallback():
    tasks, attempts = _closed_attempts("A", provenance="real")
    report = assemble_minimal_report(
        study_id="st_refused",
        terminal_reason="refused",
        trigger="intake",
        contributing_reasons=[],
        verdict="failed",
        tasks=tasks,
        attempts=attempts,
        question_dispositions={},
        fallback_reason="CASUnavailable",
        refusal="out_of_scope: catalog missing",
        hypotheses={},
        hypotheses_unavailable=None,
        custom_checks=[],
        observations=[],
    )
    md = render_markdown_report(report, tasks=tasks)
    assert "- refusal: `out_of_scope: catalog missing`" in md
    assert "- fallback_reason: `CASUnavailable`" in md


def test_fulfilled_report_has_no_honest_section():
    report, tasks = _disclaimer_report("fulfilled")
    md = render_markdown_report(report, tasks=tasks)
    assert "## Honest outcome" not in md
    assert "no scientific acceptance is asserted" not in md


def test_usage_rows_rendered_when_provided():
    report, tasks = _disclaimer_report("failed")
    md = render_markdown_report(
        report,
        tasks=tasks,
        usage={"attempts_started": "3", "spent_rub": "0.05"},
    )
    assert "## Usage" in md
    assert "- attempts_started: 3" in md
    assert "- spent_rub: 0.05" in md
    # Tokens are not journaled — the report says so instead of inventing them.
    assert "llm tokens: unavailable" in md


def test_usage_section_absent_without_usage():
    report, tasks = _disclaimer_report("fulfilled")
    md = render_markdown_report(report, tasks=tasks)
    assert "## Usage" not in md


async def _boot_with_arts(tmp_path: Path, clock, sid: str):
    rt = await _boot(tmp_path, clock, sid)
    rt.artifacts = LocalArtifactStore(tmp_path / sid / "arts")
    return rt


@pytest.mark.asyncio
async def test_cancelled_terminal_writes_report_md(tmp_path, clock):
    """Cancelled study: CAS report.md with typed cancelled reasons + disclaimer."""
    sid = "st_any_cancel"
    rt = await _boot_with_arts(tmp_path, clock, sid)
    await _skip_all(rt)
    await finalize_mod_fn(rt, trigger="cancel")
    fin = _fin(await rt.store.read_all())
    # Primary path: rich report artifact, payload contract unchanged.
    assert fin.payload["report_artifact_id"]
    md = _md_by_name(rt.artifacts)["report.md"]
    assert "verdict: `failed`" in md
    assert NON_FULFILLED_DISCLAIMER in md
    assert "reason=cancelled" in md
    assert "## Usage" in md
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_rich_crash_last_resort_still_writes_report_md(tmp_path, clock, monkeypatch):
    """Rich path crash → last resort; CAS report.md still lands (best-effort)."""
    sid = "st_any_crash"
    rt = await _boot_with_arts(tmp_path, clock, sid)
    await _skip_all(rt)

    def boom(**_k):
        raise ValueError("disp_boom")

    monkeypatch.setattr("coscientist.core.finalize.finalize.dispositions", boom)
    await finalize_mod_fn(rt, trigger="cancel")
    fin = _fin(await rt.store.read_all())
    # Payload contract: minimal_report + no report_artifact_id (unchanged XOR).
    assert fin.payload["report_artifact_id"] is None
    assert fin.payload["minimal_report"] is not None
    assert fin.payload["fallback_reason"] == "ValueError"
    md = _md_by_name(rt.artifacts)["report.md"]
    assert NON_FULFILLED_DISCLAIMER in md
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_last_resort_report_total_when_cas_write_fails(tmp_path, clock, monkeypatch):
    """Report packaging must never sink finalize (legacy totality rule)."""
    sid = "st_any_nocas"
    rt = await _boot_with_arts(tmp_path, clock, sid)
    await _skip_all(rt)

    class _BoomStore:
        def put(self, *_a, **_k):
            raise OSError("cas_boom")

    monkeypatch.setattr(
        "coscientist.core.finalize.finalize._artifact_store", lambda _rt: _BoomStore()
    )
    await finalize_mod_fn(rt, trigger="cancel")
    fin = _fin(await rt.store.read_all())
    # Primary and last-resort report writes both failed → payload stays minimal,
    # finalize still succeeded, no crash.
    assert fin.payload["report_artifact_id"] is None
    assert fin.payload["minimal_report"] is not None
    assert fin.payload["fallback_reason"] == "OSError"
    assert _md_by_name(rt.artifacts) == {}
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_intake_refused_without_study_writes_report_md(tmp_path, clock):
    """Intake refuse (no StudyCreated): CAS report.md with refusal + disclaimer."""
    sid = "st_any_refused"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    rt.artifacts = LocalArtifactStore(tmp_path / sid / "arts")
    out_sid = await rt.refuse_study_without_catalog(
        kind=RefusalKind.OUT_OF_SCOPE, reason="test refusal reason"
    )
    assert out_sid == sid
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1
    payload = fins[0].payload
    # Payload contract unchanged: constant-only last resort (minimal, no artifact id).
    assert payload["report_artifact_id"] is None
    assert payload["minimal_report"] is not None
    assert payload["refusal"] == "out_of_scope:test refusal reason"
    md = _md_by_name(rt.artifacts)["report.md"]
    assert "- refusal: `out_of_scope:test refusal reason`" in md
    assert NON_FULFILLED_DISCLAIMER in md
    await close_runtime(rt)
