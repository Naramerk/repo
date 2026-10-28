"""Interactive API path over the actual CLI runner; LLM replies are fixtures."""

import asyncio

import httpx
import pytest

import coscientist.cli.run as run_module
import tests.cli.test_run_study as run_fixtures
from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.hitl import HitlOwner
from coscientist.api.run import _RunRecord
from coscientist.cli.errors import CliRefuse
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy
from tests.api.test_hitl_owner import _waiting
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import (
    _cli_plan_out_json,
    _exec_out_json,
    _ok_body,
    _run,
    _spec_json,
)


@pytest.mark.asyncio
async def _answer_all_gates(client, sid: str, task, *, plan_feedback: str | None = None) -> None:
    """Answer EVERY pending gate until the run task settles.

    HITL v2 opens a chain (spec → plan → [report]); a test that answers only
    the first card hangs the run on the next one.
    """
    answered: set[str] = set()
    while not task.done():
        rows = (await client.get(f"/console/studies/{sid}/hitl")).json()
        pending = [r for r in rows if r["status"] == "pending" and r["request_id"] not in answered]
        if not pending:
            await asyncio.sleep(0.01)
            continue
        for row in pending:
            rid = row["request_id"]
            answered.add(rid)
            url = f"/studies/{sid}/hitl/{rid}"
            payload = dict(decision="accept", opened_seq=row["opened_seq"],
                           context_sha256=row["prompt"]["context_sha256"])
            if plan_feedback is not None and row["kind"] == "plan_approval":
                payload["feedback"] = plan_feedback
            response = await client.post(url, json=payload)
            assert response.status_code == 200, response.text
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
async def _answer_until_gate(client, sid: str, task, kind: str):
    """Accept pending gates until a pending gate of ``kind`` appears; return its row."""
    answered: set[str] = set()
    while True:
        rows = (await client.get(f"/console/studies/{sid}/hitl")).json()
        pending = [r for r in rows if r["status"] == "pending" and r["request_id"] not in answered]
        target = next((r for r in pending if r["kind"] == kind), None)
        if target is not None:
            return target
        for row in pending:
            answered.add(row["request_id"])
            response = await client.post(
                f"/studies/{sid}/hitl/{row['request_id']}",
                json=dict(decision="accept", opened_seq=row["opened_seq"],
                          context_sha256=row["prompt"]["context_sha256"]),
            )
            assert response.status_code == 200, response.text
        if task.done():
            raise AssertionError(f"run settled before a {kind} gate opened")
        await asyncio.sleep(0.01)





@pytest.mark.parametrize("decision", ["accept", "reject"])
async def test_api_interactive_runner_waits_for_human(tmp_path, monkeypatch, decision):
    sid = "st_" + "a" * 27
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    original = run_module.run_study

    async def controlled(**kwargs):
        kwargs["control"] = owner
        kwargs["hitl_policy"] = HitlPolicy(mode="interactive")
        try:
            return await original(**kwargs)
        finally:
            owner.close()

    monkeypatch.setattr(run_fixtures, "run_study", controlled)
    bodies = [_ok_body(content=_spec_json()), _ok_body(content=_cli_plan_out_json()),
              _ok_body(content=_exec_out_json(tool="dataset_overview")),
              _ok_body(content=_exec_out_json(tool="chemical_space_clustering"))]
    task = asyncio.create_task(_run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, study_id=sid))
    await _waiting(owner, task)
    deps = bind_app_deps(tmp_path / "data", catalog=minimal_test_catalog(), clock=FakeClock(),
                         stream_poll_interval_s=0.01, stream_sleep=asyncio.sleep, run_wait_s=2)
    deps.run_studies._runs[sid] = _RunRecord(sid, "running", None, None, task, owner, None)
    app = create_app(deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        row = await _answer_until_gate(client, sid, task, "plan_approval")
        url = f"/studies/{sid}/hitl/{row['request_id']}"
        payload = dict(decision=decision, opened_seq=row["opened_seq"],
                       context_sha256=row["prompt"]["context_sha256"])
        stale = await client.post(url, json={**payload, "opened_seq": row["opened_seq"] + 1})
        assert stale.status_code == 409
        response = await client.post(url, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == decision
        if decision == "accept":
            # HITL v2 opens a chain (spec → plan → report): keep answering
            # until the run settles — one card is never the whole chain.
            await _answer_all_gates(client, sid, task)
            assert (await task)[1]["verdict"] == "fulfilled"
        else:
            with pytest.raises(CliRefuse, match="human"):
                await task
        replay = (await client.get(f"/console/studies/{sid}/hitl")).json()
        decided_rows = [r for r in replay if r["request_id"] == row["request_id"]]
        assert decided_rows and decided_rows[0]["status"] == "answered"
        assert (await client.post(url, json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_api_answer_feedback_contract_422_and_plan_revise_refused(tmp_path, monkeypatch):
    """HITL v2 contract: revise without feedback is 422 at the wire; revise with
    feedback on a plan gate is a typed 409 (stage-gate mechanic only)."""
    sid = "st_" + "b" * 27
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    original = run_module.run_study

    async def controlled(**kwargs):
        kwargs["control"] = owner
        kwargs["hitl_policy"] = HitlPolicy(mode="interactive")
        try:
            return await original(**kwargs)
        finally:
            owner.close()

    monkeypatch.setattr(run_fixtures, "run_study", controlled)
    bodies = [_ok_body(content=_spec_json()), _ok_body(content=_cli_plan_out_json()),
              _ok_body(content=_exec_out_json(tool="dataset_overview")),
              _ok_body(content=_exec_out_json(tool="chemical_space_clustering"))]
    task = asyncio.create_task(_run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, study_id=sid))
    await _waiting(owner, task)
    deps = bind_app_deps(tmp_path / "data", catalog=minimal_test_catalog(), clock=FakeClock(),
                         stream_poll_interval_s=0.01, stream_sleep=asyncio.sleep, run_wait_s=2)
    deps.run_studies._runs[sid] = _RunRecord(sid, "running", None, None, task, owner, None)
    app = create_app(deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        row = await _answer_until_gate(client, sid, task, "plan_approval")
        url = f"/studies/{sid}/hitl/{row['request_id']}"
        identity = dict(opened_seq=row["opened_seq"], context_sha256=row["prompt"]["context_sha256"])
        missing = await client.post(url, json={"decision": "revise", **identity})
        assert missing.status_code == 422
        long_feedback = await client.post(
            url, json={"decision": "reject", **identity, "feedback": "x" * 4001},
        )
        assert long_feedback.status_code == 422
        revise = await client.post(url, json={"decision": "revise", **identity, "feedback": "поменяй"})
        assert revise.status_code == 409
        assert revise.json()["detail"] == "hitl_revise_needs_stage_gate"
        rows = (await client.get(f"/console/studies/{sid}/hitl")).json()
        assert [r for r in rows if r["request_id"] == row["request_id"]][0]["status"] == "pending"
        accepted = await client.post(url, json={"decision": "accept", **identity, "feedback": "ок"})
        assert accepted.status_code == 200
        await _answer_all_gates(client, sid, task)
        assert (await task)[1]["verdict"] == "fulfilled"
        decided = [r for r in (await client.get(f"/console/studies/{sid}/hitl")).json()
                   if r["request_id"] == row["request_id"]][0]["answer"]
        assert decided["feedback"] == "ок"
