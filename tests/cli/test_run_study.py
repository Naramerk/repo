"""CLI ``run_study`` with fake adapter seams — closes by deadline; usage written."""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.run import run_study
from coscientist.core.events.clock import FakeClock
from coscientist.llm.client import LlmClient
from tests.agents.test_adapter_roles import (
    CassetteTransport,
    FakeToolRouter,
    _exec_out_json,
    _plan_out_json,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body


@pytest.mark.asyncio
async def test_run_study_writes_usage_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    task = tmp_path / "two_tasks.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    bodies = [
        _ok_body(content=_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(
            content=_exec_out_json(tool="dataset_overview"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    llm_clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable, *, timeout_s: float):
        del timeout_s
        return await awaitable

    client = LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=llm_clock,
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )
    router = FakeToolRouter(
        tools={
            "dataset_overview": {"ok": True},
            "chemical_space_clustering": {"ok": True},
        }
    )
    adapter = LlmAgentAdapter(
        client,
        role_models={"planner": "test-model", "executor-route": "test-model"},
        tool_router=router,
    )
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        profile="research",
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_clock=FakeClock(),
    )
    summary = json.loads(out.getvalue())
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["usage"][0]["role"] == "planner"
    assert summary["usage"][0]["prompt_tokens"] == 10
    assert summary["usage"][0]["completion_tokens"] == 20
    assert any(u["role"] == "executor-route" for u in summary["usage"])
    assert router.calls == ["dataset_overview", "chemical_space_clustering"]
