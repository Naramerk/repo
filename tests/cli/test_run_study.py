"""CLI ``run_study`` — honest journal cycle; usage written; negatives refuse."""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import run_study
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import Need
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


def _spec_json() -> str:
    return ResearchSpec(
        object="two_tasks study",
        goal=Goal(statement="complete two experiment tasks", research_form=None, trl=None),
        scope="synthetic",
        constraints=(Constraint(subtype="resource", content="cassette LLM", limit=None),),
        success_metrics=(
            SuccessMetric(metric="tasks", op="==", target="2", check="code", tolerance=None),
        ),
    ).model_dump_json()


def _adapter(client: LlmClient, router: FakeToolRouter) -> LlmAgentAdapter:
    return LlmAgentAdapter(
        client,
        role_models={
            "planner": "test-model",
            "executor-route": "test-model",
            "tz_spec": "test-model",
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
    )


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    llm_clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable, *, timeout_s: float):
        del timeout_s
        return await awaitable

    return LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=llm_clock,
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )


async def _run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    require_hypotheses: bool = False,
    router: FakeToolRouter | None = None,
) -> tuple[int, dict, FakeToolRouter]:
    task = tmp_path / "two_tasks.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    r = router or FakeToolRouter(
        tools={
            "dataset_overview": {"ok": True},
            "chemical_space_clustering": {"ok": True},
        }
    )
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), r)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        profile="research",
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_id="two_tasks_study",
        require_hypotheses=require_hypotheses,
        finalize_reserve_s=30,
        lease_ttl_s=60.0,
        max_llm_calls=100,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        study_clock=FakeClock(),
    )
    summary = json.loads(out.getvalue()) if out.getvalue().strip() else {}
    return code, summary, r


@pytest.mark.asyncio
async def test_run_study_fulfilled_records_executor_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
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
    code, summary, router = await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"
    assert summary["usage"][0]["role"] == "tz_spec"
    assert any(u["role"] == "planner" for u in summary["usage"])
    assert any(u["role"] == "executor-route" for u in summary["usage"])
    assert [name for name, _args in router.calls] == [
        "dataset_overview",
        "chemical_space_clustering",
    ]


@pytest.mark.asyncio
async def test_require_hypotheses_true_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CliRefuse, match="hypothesizer") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=[], require_hypotheses=True)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_planner_hypotheses_needed_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coscientist.agents.schemas import (
        OutputSpecDraft,
        PlanOut,
        QuestionDraft,
        TaskSpecDraft,
    )

    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="verdict"),),
        hypotheses=Need(kind="need", needed=True, reason="want hyp"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan.model_dump_json()),
    ]
    with pytest.raises(CliRefuse, match="hypothesizer") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_empty_task_file_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = tmp_path / "empty.txt"
    task.write_text("   ", encoding="utf-8")
    adapter = _adapter(_client([], monkeypatch=monkeypatch), FakeToolRouter(tools={}))
    with pytest.raises(CliRefuse, match="non-empty") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            profile="research",
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="s",
            require_hypotheses=False,
            finalize_reserve_s=1,
            lease_ttl_s=30.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
            study_clock=FakeClock(),
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_planner_literature_needed_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coscientist.agents.schemas import (
        OutputSpecDraft,
        PlanOut,
        QuestionDraft,
        TaskSpecDraft,
    )

    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=True, reason="want lit"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan.model_dump_json()),
    ]
    with pytest.raises(CliRefuse, match="researcher") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_accept_plan_invalid_dag_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coscientist.agents.schemas import (
        OutputSpecDraft,
        PlanOut,
        QuestionDraft,
        TaskSpecDraft,
    )

    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t0",
                goal="g",
                route="experiment",
                depends_on=("EXP-1",),
                addresses=("Q1",),
                tools_hint=("dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
            TaskSpecDraft(
                task_key="EXP-1",
                title="t1",
                goal="g",
                route="experiment",
                depends_on=("EXP-0",),
                addresses=("Q1",),
                tools_hint=("chemical_space_clustering",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan.model_dump_json()),
    ]
    with pytest.raises(CliRefuse, match="accept_plan refused") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_executor_route_bad_schema_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
    ]
    with pytest.raises(CliRefuse, match="agent error|schema validation") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 4


def test_main_missing_required_args_exits_nonzero() -> None:
    from coscientist.cli.run import main

    with pytest.raises(SystemExit) as ei:
        main(["run", "missing_task.txt"])
    assert ei.value.code != 0


def test_cli_refuse_rejects_zero_code() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        CliRefuse("x", code=0)
