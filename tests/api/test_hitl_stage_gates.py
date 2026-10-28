"""Interactive stage gates (HITL v2, hitl-map §5) over the real run_study loop.

Spec gate before PLAN, HYP/LIT gates after their wired stages, report gate as
the finalize barrier; LLM replies are cassettes, the human answers through the
live HitlOwner inbox (the same submit path the HTTP route drives).
"""

from __future__ import annotations

import asyncio
import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from coscientist.api.console import read_events
from coscientist.api.hitl import AnswerBody, HitlOwner
from coscientist.api.hitl_routes import hitl_rows
from coscientist.cli.run import run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy, Need
from coscientist.llm.client import LlmClient
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_hyp_lit_wiring import (
    _adapter as _wiring_adapter,
)
from tests.cli.test_hyp_lit_wiring import (
    _hyp_out_json,
    _lit_out_json,
    _lit_tools,
)
from tests.cli.test_hyp_lit_wiring import (
    _intent_json as _wiring_intent,
)
from tests.cli.test_report_lane import _nir_body, _science_body, _tz_body
from tests.cli.test_run_study import CATALOG_VERSION, _cli_plan_out_json, _spec_json
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_T = TypeVar("_T")

_PROFILE = load_profile("research", expected_tool_catalog_version=2)


def _client_and_transport(
    bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch
) -> tuple[LlmClient, CassetteTransport]:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
        del timeout_s
        return await awaitable

    transport = CassetteTransport(bodies)
    client = LlmClient(
        _config(max_retries=0),
        transport,
        clock=LlmFakeClock(),
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )
    return client, transport


def _tools() -> StaticToolRouter:
    return StaticToolRouter(tools=_lit_tools())


async def _run_interactive(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    owner: HitlOwner,
    wired_roles: frozenset[str],
    study_id: str,
    router: StaticToolRouter | None = None,
) -> asyncio.Task[None]:
    """Start run_study with the live owner; the test answers its gates."""
    task_file = tmp_path / "task.txt"
    task_file.write_text("Study the sotorasib literature and hypothesize.", encoding="utf-8")
    r = router if router is not None else _tools()
    client, _transport = _client_and_transport(bodies, monkeypatch=monkeypatch)
    out = io.StringIO()

    async def _run() -> None:
        code = await run_study(
            task_path=task_file,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_PROFILE,
            owner="hitl-stage-gates-test",
            adapter=_wiring_adapter(client, r),
            out=out,
            study_id=study_id,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=40,
            tools=r,
            hitl_policy=HitlPolicy(mode="interactive"),
            wired_roles=wired_roles,
            study_clock=FakeClock(),
            llm_client=client,
            control=owner,
        )
        assert code == 0, f"run_study did not fulfill: {out.getvalue()}"

    return asyncio.create_task(_run())


def _rows(tmp_path: Path, sid: str) -> list[dict[str, Any]]:
    return hitl_rows(read_events(tmp_path / "data", sid, 0))


def _gate_kinds(tmp_path: Path, sid: str) -> list[str]:
    return [str(row["kind"]) for row in _rows(tmp_path, sid)]


async def _answer_pending(
    owner: HitlOwner,
    tmp_path: Path,
    sid: str,
    run_task: asyncio.Task[None],
    decision: str,
    *,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Wait for the newest pending gate and answer it through the owner inbox."""
    async with asyncio.timeout(10):
        while True:
            if run_task.done():
                await run_task
                raise AssertionError("run finished with a gate unanswered")
            if owner.waiting is not None and owner.waiting[0] == sid:
                break
            await asyncio.sleep(0)
    pending = [row for row in _rows(tmp_path, sid) if row["status"] == "pending"]
    assert pending, "owner is waiting but no pending gate row is visible"
    row = pending[-1]
    body: dict[str, Any] = dict(
        decision=decision, opened_seq=row["opened_seq"],
        context_sha256=row["prompt"]["context_sha256"],
    )
    if feedback is not None:
        body["feedback"] = feedback
    assert owner.waiting is not None
    return await owner.submit(*owner.waiting, AnswerBody(**body))


def _science_markdown(tmp_path: Path, sid: str) -> str:
    meta_dir = tmp_path / "data" / sid / "arts" / "meta"
    for meta_file in sorted(meta_dir.glob("*.json")):
        row = json.loads(meta_file.read_text(encoding="utf-8"))
        if row["name"] == "report.science.md":
            return (tmp_path / "data" / sid / "arts" / "cas" / str(row["sha256"])).read_text(encoding="utf-8")
    raise AssertionError("report.science.md artifact not found")


@pytest.mark.asyncio
async def test_spec_gate_opens_before_plan_and_accepts(tmp_path, monkeypatch):
    """Gate 1: spec_approval opens after SPEC success, before PLAN; then the
    plan_approval gate keeps its place (order pinned)."""
    sid = "st_stage_spec"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset(), study_id=sid,
    )
    try:
        ack = await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        assert ack["decision"] == "accept"
        plan_ack = await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        assert plan_ack["decision"] == "accept"
        await asyncio.wait_for(run_task, timeout=20)
        assert _gate_kinds(tmp_path, sid) == ["spec_approval", "plan_approval"]
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_spec_revise_with_feedback_repairs_then_accepts(tmp_path, monkeypatch):
    """revise + feedback → the tz_spec role re-prompts with the exact text (one
    repair round); the corrected spec opens the second gate round."""
    sid = "st_stage_spec_fix"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_spec_json()),  # the repair round's corrected output
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset(), study_id=sid,
    )
    try:
        await _answer_pending(
            owner, tmp_path, sid, run_task, "revise", feedback="сузь объём работ",
        )
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        await asyncio.wait_for(run_task, timeout=20)
        spec_gates = [row for row in _rows(tmp_path, sid) if row["kind"] == "spec_approval"]
        assert len(spec_gates) == 2, "one repair round must reopen the spec gate once"
        decided = [
            row["answer"] for row in spec_gates
            if row.get("answer", {}).get("decision") == "revise"
        ]
        assert decided and decided[0]["feedback"] == "сузь объём работ"
    finally:
        owner.close()


def _hyp_task_intent():
    from coscientist.agents.schemas import TaskIntent

    return TaskIntent(
        question_indices=(1,),
        goal="state hypotheses",
        route="hypotheses",
        key="HYP-0",
        tools_hint=("static:dataset_overview",),
    )


def _exp_task_intent():
    from coscientist.agents.schemas import TaskIntent

    return TaskIntent(
        question_indices=(1,),
        goal="overview",
        route="experiment",
        key="EXP-0",
        tools_hint=("static:dataset_overview",),
    )


@pytest.mark.asyncio
async def test_hyp_gate_follows_the_hyp_stage(tmp_path, monkeypatch):
    """Gate 2: hypotheses after the HYP stage output, before plan_approval."""
    sid = "st_stage_hyp"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    plan = _wiring_intent(
        (_hyp_task_intent(),),
        hypotheses=Need(kind="need", needed=True, reason="request asks for hypotheses"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        _ok_body(content=_hyp_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset({"hypothesizer"}), study_id=sid,
    )
    try:
        for decision in ("accept", "accept", "accept"):
            await _answer_pending(owner, tmp_path, sid, run_task, decision)
        await asyncio.wait_for(run_task, timeout=20)
        assert _gate_kinds(tmp_path, sid) == ["spec_approval", "hypotheses", "plan_approval"]
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_lit_gate_follows_the_lit_stage(tmp_path, monkeypatch):
    """Gate 4: literature after the LIT stage output, before plan_approval."""
    sid = "st_stage_lit"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    plan = _wiring_intent(
        (_exp_task_intent(),),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        _ok_body(content=_lit_out_json(tool="static:search_papers", identity_key="10.1/abc")),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset({"researcher"}), study_id=sid,
    )
    try:
        for decision in ("accept", "accept", "accept"):
            await _answer_pending(owner, tmp_path, sid, run_task, decision)
        await asyncio.wait_for(run_task, timeout=20)
        assert _gate_kinds(tmp_path, sid) == ["spec_approval", "literature", "plan_approval"]
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_lit_gate_skipped_when_stage_not_needed(tmp_path, monkeypatch):
    """Skip silently: not-needed LIT (and HYP) → no gates for them; only the
    spec gate and the plan gate open."""
    sid = "st_stage_lit_off"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),  # hypotheses/literature need-off
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset({"hypothesizer", "researcher"}), study_id=sid,
    )
    try:
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")
        await asyncio.wait_for(run_task, timeout=20)
        assert _gate_kinds(tmp_path, sid) == ["spec_approval", "plan_approval"]
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_report_gate_sits_before_finalize(tmp_path, monkeypatch):
    """Gate 5: the report gate opens on the rendered draft BEFORE StudyFinalized;
    accept closes REPORT → finalize; no post-finalize re-render (cassette exact)."""
    sid = "st_stage_report"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
        _science_body(),
        _tz_body(),
        _nir_body(),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset({"report"}), study_id=sid,
    )
    try:
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # spec
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # plan
        ack = await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # report
        assert ack["decision"] == "accept"
        await asyncio.wait_for(run_task, timeout=30)
        assert _gate_kinds(tmp_path, sid) == ["spec_approval", "plan_approval", "report"]
        events = read_events(tmp_path / "data", sid, 0)
        opened_kinds = [e["payload"]["kind"] for e in events if e["type"] == "HitlOpened"]
        assert opened_kinds == ["spec_approval", "plan_approval", "report"]
        finalized_seq = next(e["seq"] for e in events if e["type"] == "StudyFinalized")
        report_seq = next(
            e["seq"] for e in events
            if e["type"] == "HitlOpened" and e["payload"]["kind"] == "report"
        )
        assert report_seq < finalized_seq
        markdown = _science_markdown(tmp_path, sid)
        assert "Замечания человека" not in markdown  # accepted without comments
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_report_gate_revise_renders_once_then_finalizes_anyway(tmp_path, monkeypatch):
    """revise + feedback → ONE repair render with the human text; the second
    gate accepts; the final science report folds the feedback verbatim into
    «Замечания человека» (report exists at any terminal)."""
    sid = "st_stage_report_fix"
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
        _science_body(),
        _tz_body(),
        _nir_body(),
        _science_body(text_extra=" (после замечаний человека)"),
        _tz_body(),
        _nir_body(),
    ]
    run_task = await _run_interactive(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, owner=owner,
        wired_roles=frozenset({"report"}), study_id=sid,
    )
    try:
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # spec
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # plan
        await _answer_pending(  # report round 1: revise with the human text
            owner, tmp_path, sid, run_task, "revise",
            feedback="добавь ограничения инструментов",
        )
        await _answer_pending(owner, tmp_path, sid, run_task, "accept")  # round 2
        await asyncio.wait_for(run_task, timeout=30)
        report_rows = [row for row in _rows(tmp_path, sid) if row["kind"] == "report"]
        assert len(report_rows) == 2, "one repair round must reopen the report gate once"
        markdown = _science_markdown(tmp_path, sid)
        assert "## Замечания человека" in markdown
        assert "добавь ограничения инструментов" in markdown
    finally:
        owner.close()
