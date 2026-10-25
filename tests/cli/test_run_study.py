"""CLI ``run_study`` — honest journal cycle; usage written; negatives refuse."""

from __future__ import annotations

import argparse
import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import (
    OutputSpecDraft,
    PlanOut,
    QuestionDraft,
    RubricDraft,
    RubricItemDraft,
    TaskSpecDraft,
)
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.run import main, run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.errors import LlmTransportError
from coscientist.profiles.load import load_profile
from coscientist.tools.router import ToolNeed
from tests.agents.test_adapter_roles import (
    CassetteTransport,
    _exec_out_json,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_RESEARCH_PROFILE = load_profile("research", expected_tool_catalog_version=1)
_RESEARCH_MODEL = _RESEARCH_PROFILE.roles["planner"].model

_T = TypeVar("_T")


def _spec_json() -> str:
    return ResearchSpec(
        object="two_tasks study",
        goal=Goal(statement="complete two experiment tasks", research_form=None, trl=None),
        scope="synthetic",
        constraints=(Constraint(subtype="resource", content="cassette LLM", limit=None),),
        success_metrics=(SuccessMetric(metric="tasks", op="==", target="2", check="code", tolerance=None),),
    ).model_dump_json()


def _artifact_rubric() -> RubricDraft:
    return RubricDraft(
        items=(
            RubricItemDraft(
                id="exists",
                text="required artifact exists",
                required=True,
                check="code",
                code_kind="artifact_exists",
            ),
        ),
        aggregate="all_required",
        k=None,
    )


def _cli_plan_out_json() -> str:
    """Plan cassette for StaticToolRouter — tools_hint are exact ``static:<tool>``."""
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="Dataset overview",
                goal="overview",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
            ),
            TaskSpecDraft(
                task_key="EXP-1",
                title="Chemical space clustering",
                goal="cluster",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:chemical_space_clustering",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="two tasks?", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=False, reason="not required for two_tasks"),
        literature=Need(kind="need", needed=False, reason="not required for two_tasks"),
    )
    return plan.model_dump_json()


def _adapter(client: LlmClient, router: StaticToolRouter) -> LlmAgentAdapter:
    return LlmAgentAdapter(
        client,
        role_models={
            "planner": _RESEARCH_MODEL,
            "executor-route": _RESEARCH_MODEL,
            "tz_spec": _RESEARCH_MODEL,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    llm_clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
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


def _cassette_tool(result: dict[str, object] | None = None) -> dict[str, object]:
    return {"result": dict(result or {"ok": True}), "args": {}}


def _default_tools() -> dict[str, dict[str, object]]:
    return {
        "dataset_overview": _cassette_tool(),
        "chemical_space_clustering": _cassette_tool(),
    }


async def _run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    require_hypotheses: bool = False,
    confirmations_needed: int = 1,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
    max_expansions: int = 2,
    router: StaticToolRouter | None = None,
    max_ticks: int = 20,
    deadline_s: float = 3600.0,
    study_id: str = "two_tasks_study",
    catalog_version: int = CATALOG_VERSION,
    finalize_reserve_s: int = 30,
    lease_ttl_s: float = 60.0,
    max_llm_calls: int = 100,
    max_tokens: int = 1_000_000,
    max_tool_calls: int = 50,
    task_text: str = "Build dataset overview and chemical space clustering.",
) -> tuple[int, dict[str, object], StaticToolRouter]:
    task = tmp_path / "two_tasks.txt"
    task.write_text(task_text, encoding="utf-8")
    if router is None:
        r = StaticToolRouter(tools=_default_tools())
    else:
        r = router
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), r)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=deadline_s,
        catalog_version=catalog_version,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_id=study_id,
        require_hypotheses=require_hypotheses,
        confirmations_needed=confirmations_needed,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
        max_expansions=max_expansions,
        finalize_reserve_s=finalize_reserve_s,
        lease_ttl_s=lease_ttl_s,
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_tool_calls=max_tool_calls,
        max_ticks=max_ticks,
        tools=r,
        hitl_policy=HitlPolicy(mode="headless"),
        study_clock=FakeClock(),
    )
    raw = out.getvalue().strip()
    if not raw:
        return code, {}, r
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise AssertionError(f"run_study summary must be a JSON object, got {type(loaded).__name__}")
    summary: dict[str, object] = {str(k): v for k, v in loaded.items()}
    return code, summary, r


@pytest.mark.asyncio
async def test_run_study_fulfilled_records_executor_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
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
    assert isinstance(router, StaticToolRouter)
    assert router.tool_names == frozenset({"static:dataset_overview", "static:chemical_space_clustering"})


@pytest.mark.asyncio
async def test_require_hypotheses_true_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CliRefuse, match="hypothesizer") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=[], require_hypotheses=True)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_planner_hypotheses_needed_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="artifact"),),
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
    with pytest.raises(CliRefuse, match="non-empty") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            task_text="   ",
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("deadline_s", 0.0, "deadline_s must be > 0"),
        ("study_id", "  ", "study_id must be a non-empty str"),
        ("finalize_reserve_s", -1, "finalize_reserve_s must be int >= 0"),
        ("lease_ttl_s", 0.0, "lease_ttl_s must be > 0"),
        ("max_ticks", 0, "max_ticks must be int >= 1"),
        ("confirmations_needed", 0, "confirmations_needed must be int >= 1"),
        ("refutations_needed", 0, "refutations_needed must be int >= 1"),
        ("supported_needed", 1, "supported_needed must be int >= 2"),
        ("max_expansions", -1, "max_expansions must be int >= 0"),
    ],
    ids=[
        "deadline_s",
        "study_id",
        "finalize_reserve_s",
        "lease_ttl_s",
        "max_ticks",
        "confirmations_needed",
        "refutations_needed",
        "supported_needed",
        "max_expansions",
    ],
)
async def test_run_study_arg_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: float | int | str,
    match: str,
) -> None:
    with pytest.raises(CliRefuse, match=match) as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=[], **{field: value})
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_catalog_version_non_int_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools={"dataset_overview": _cassette_tool()})
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router)
    extras: dict[str, object] = {"catalog_version": "1"}
    with pytest.raises(CliRefuse, match="catalog_version must be int") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="s",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=1,
            lease_ttl_s=30.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
            **extras,  # catalog_version deliberately wrong type
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_planner_literature_needed_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
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
async def test_accept_plan_invalid_dag_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t0",
                goal="g",
                route="experiment",
                depends_on=("EXP-1",),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
            ),
            TaskSpecDraft(
                task_key="EXP-1",
                title="t1",
                goal="g",
                route="experiment",
                depends_on=("EXP-0",),
                addresses=("Q1",),
                tools_hint=("static:chemical_space_clustering",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
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
async def test_executor_route_bad_schema_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
    ]
    with pytest.raises(CliRefuse, match="agent error|schema validation") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_max_ticks_exhausted_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.api.runtime import StudyRuntime

    async def _noop_tick(self: StudyRuntime) -> None:
        del self
        return None

    monkeypatch.setattr(StudyRuntime, "tick", _noop_tick)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
    ]
    with pytest.raises(CliRefuse, match="max_ticks=") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, max_ticks=1)
    assert ei.value.code == 6


@pytest.mark.asyncio
async def test_tools_hint_outside_allow_list_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("unknown_tool",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
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
    with pytest.raises(CliRefuse, match="not in router allow-list") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 3


def test_main_missing_required_args_exits_nonzero() -> None:
    with pytest.raises(SystemExit, match="2") as ei:
        main(["run", "missing_task.txt"])
    assert ei.value.code == 2


def _main_argv(tmp_path: Path, *, hitl_mode: str | None) -> list[str]:
    """Minimal valid ``coscientist run`` argv; omit ``--hitl-mode`` when None."""
    task = tmp_path / "task.txt"
    task.write_text("study request", encoding="utf-8")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(minimal_test_catalog().model_dump_json(), encoding="utf-8")
    llm_path = tmp_path / "llm.json"
    llm_path.write_text(_config().model_dump_json(), encoding="utf-8")
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(json.dumps(_default_tools()), encoding="utf-8")
    argv = [
        "run",
        str(task),
        "--data-root",
        str(tmp_path / "data"),
        "--deadline-s",
        "60",
        "--catalog-version",
        str(CATALOG_VERSION),
        "--catalog",
        str(catalog_path),
        "--profile",
        "research",
        "--tool-catalog-version",
        "1",
        "--owner",
        "cli-test",
        "--llm-config",
        str(llm_path),
        "--planner-model",
        _RESEARCH_MODEL,
        "--executor-model",
        _RESEARCH_MODEL,
        "--tz-spec-model",
        _RESEARCH_MODEL,
        "--tools-json",
        str(tools_path),
        "--study-id",
        "hitl_mode_study",
        "--require-hypotheses",
        "false",
        "--confirmations-needed",
        "1",
        "--refutations-needed",
        "1",
        "--refutation-priority",
        "false",
        "--supported-needed",
        "2",
        "--max-expansions",
        "2",
        "--finalize-reserve-s",
        "30",
        "--lease-ttl-s",
        "60",
        "--rng-seed",
        "0",
        "--max-llm-calls",
        "10",
        "--max-tokens",
        "1000",
        "--max-tool-calls",
        "10",
        "--max-ticks",
        "5",
    ]
    if hitl_mode is not None:
        argv.extend(["--hitl-mode", hitl_mode])
    return argv


def test_main_missing_hitl_mode_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    with pytest.raises(SystemExit, match="2") as ei:
        main(_main_argv(tmp_path, hitl_mode=None))
    assert ei.value.code == 2


@pytest.mark.parametrize(
    "mode",
    ["interactive", "auto", "headless"],
    ids=["interactive", "auto", "headless"],
)
def test_main_hitl_mode_wires_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    seen: dict[str, object] = {}

    async def _capture_run_study(**kwargs: object) -> int:
        seen["hitl_policy"] = kwargs["hitl_policy"]
        return 0

    monkeypatch.setattr("coscientist.cli.run.run_study", _capture_run_study)
    monkeypatch.setattr(
        "coscientist.cli.run.load_catalog",
        lambda *args, **kwargs: minimal_test_catalog(),
    )
    code = main(_main_argv(tmp_path, hitl_mode=mode))
    assert code == 0
    policy = seen["hitl_policy"]
    assert isinstance(policy, HitlPolicy)
    assert policy.mode == mode


def test_parse_bool_direct() -> None:
    from coscientist.cli.run import _parse_bool

    assert _parse_bool("true") is True
    assert _parse_bool("false") is False
    with pytest.raises(argparse.ArgumentTypeError, match="true\\|false"):
        _parse_bool("yes")


def test_cli_refuse_rejects_zero_code() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        CliRefuse("x", code=0)


def test_static_tool_router_resolve_and_refuse_args() -> None:
    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    assert router.tool_names == frozenset({"static:t1"})
    found = router.resolve(ToolNeed(kind="by_name", value="static:t1"))
    assert found.status == "tool_found"
    assert found.server_name == "static"
    assert found.tool_name == "t1"
    assert router.resolve(ToolNeed(kind="by_name", value="t1")).status == "tool_missing"
    assert router.resolve(ToolNeed(kind="by_name", value="missing")).status == "tool_missing"


@pytest.mark.asyncio
async def test_static_tool_router_call_empty_args_ok() -> None:
    from coscientist.core.execution.domain import HitlPolicy
    from coscientist.tools.contract import CallCtx

    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    ctx = CallCtx(study_id="s", task_key="k", attempt_id="a", hitl_policy=HitlPolicy(mode="headless"))
    result = await router.call("static", "t1", {}, ctx)
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_static_tool_router_call_nonempty_args_refuses() -> None:
    from coscientist.core.execution.domain import HitlPolicy
    from coscientist.tools.contract import CallCtx

    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    ctx = CallCtx(study_id="s", task_key="k", attempt_id="a", hitl_policy=HitlPolicy(mode="headless"))
    with pytest.raises(ValueError, match="args mismatch"):
        await router.call("static", "t1", {"x": 1}, ctx)


@pytest.mark.asyncio
async def test_http_transport_timeout_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match="timeout") as ei:
        await transport.request("POST", "https://example.test", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_http_transport_invalid_url_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise httpx.InvalidURL("bad")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match="InvalidURL") as ei:
        await transport.request("POST", "not-a-url", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "retryable", "match"),
    [
        ("connect", True, "ConnectError"),
        ("remote_protocol", True, "RemoteProtocolError"),
        ("http_status", False, "HTTPStatusError"),
    ],
    ids=["network", "remote_protocol", "http_error"],
)
async def test_http_transport_error_retryable_branches(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retryable: bool,
    match: str,
) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            if kind == "http_status":
                request = httpx.Request("POST", "https://example.test")
                response = httpx.Response(500, request=request)
                raise httpx.HTTPStatusError("boom", request=request, response=response)
            if kind == "remote_protocol":
                raise httpx.RemoteProtocolError("boom")
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match=match) as ei:
        await transport.request("POST", "https://example.test", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is retryable


def test_wall_llm_clock_methods() -> None:
    clock = WallLlmClock()
    assert clock.monotonic() >= 0.0
    assert clock.wall_utc().tzinfo is not None


@pytest.mark.asyncio
async def test_wall_sleep_and_wait_for() -> None:
    await wall_sleep(0.0)
    result = await wall_wait_for(asyncio_sleep_zero(), timeout_s=1.0)
    assert result == 1


async def asyncio_sleep_zero() -> int:
    return 1


@pytest.mark.asyncio
async def test_wired_hypothesizer_without_adapter_schema_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI seam accepts --wired-roles; adapter schema hard-dep refuses (no fake SUCCESS)."""
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=True, reason="want hyp"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan.model_dump_json()),
    ]
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="hard dep: adapter role") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_hyp",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"hypothesizer"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_wired_report_without_adapter_schema_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    task = tmp_path / "t.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="report declared in --wired-roles") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_report",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"report"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 5


def test_parse_wired_roles() -> None:
    from coscientist.cli.run import _parse_wired_roles

    assert _parse_wired_roles("[]") == frozenset()
    assert _parse_wired_roles('["hypothesizer","report"]') == frozenset({"hypothesizer", "report"})
    with pytest.raises(CliRefuse, match="unknown optional role") as ei:
        _parse_wired_roles('["critic"]')
    assert ei.value.code == 2
    with pytest.raises(CliRefuse, match="JSON array") as ei2:
        _parse_wired_roles("hypothesizer")
    assert ei2.value.code == 2


def test_main_tools_json_and_mcp_bundle_mutex(tmp_path: Path) -> None:
    argv = _main_argv(tmp_path, hitl_mode="headless")
    argv.extend(["--mcp-tool-bundle", str(tmp_path / "bundle.json")])
    with pytest.raises(SystemExit, match="2") as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_mcp_bundle_requires_tool_call_args(tmp_path: Path) -> None:
    argv = _main_argv(tmp_path, hitl_mode="headless")
    # Replace --tools-json with --mcp-tool-bundle without --tool-call-args.
    tools_idx = argv.index("--tools-json")
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}", encoding="utf-8")
    argv[tools_idx] = "--mcp-tool-bundle"
    argv[tools_idx + 1] = str(bundle)
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


class _ArgsGapSurface:
    """Allow-list advertises a name ``planned_calls`` cannot satisfy (pre-fix Live bug)."""

    def __init__(self, base: StaticToolRouter, *, gap: str) -> None:
        self._base = base
        self._gap = gap

    @property
    def tool_names(self) -> frozenset[str]:
        return self._base.tool_names | frozenset({self._gap})

    def named_tools(self) -> list[object]:
        from coscientist.tools.contract import ToolSpec

        class _Named:
            def __init__(self, name: str) -> None:
                self.spec = ToolSpec(name=name)

        return list(self._base.named_tools()) + [_Named(self._gap)]

    def planned_calls(self, hints: object) -> tuple[object, ...]:
        return self._base.planned_calls(hints)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_planned_calls_keyerror_refuses_before_start_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tools_hint in allow-list but missing call args → CliRefuse, no EXP AttemptStarted."""
    from coscientist.api.runtime import StudyRuntime

    gap = "static:ghost"
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=(gap,),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=(),
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
    started: list[str] = []
    real_start = StudyRuntime.start_attempt

    async def _track_start(self: StudyRuntime, task_key_or_id: str, **kwargs: object) -> str | None:
        aid = await real_start(self, task_key_or_id, **kwargs)  # type: ignore[arg-type]
        started.append(str(task_key_or_id))
        return aid

    monkeypatch.setattr(StudyRuntime, "start_attempt", _track_start)
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    surface = _ArgsGapSurface(router, gap=gap)
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="missing declared call args") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="args_gap",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=surface,  # type: ignore[arg-type]
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 3
    assert "EXP-0" not in started


@pytest.mark.asyncio
async def test_wired_role_with_adapter_support_refuses_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supports_role True but no CLI journal → CliRefuse before HYP start_attempt."""
    from coscientist.api.runtime import StudyRuntime

    real_supports = LlmAgentAdapter.supports_role

    @staticmethod
    def _support(role: str) -> bool:
        if role == "hypothesizer":
            return True
        return real_supports(role)

    monkeypatch.setattr(LlmAgentAdapter, "supports_role", _support)
    started: list[str] = []
    real_start = StudyRuntime.start_attempt

    async def _track_start(self: StudyRuntime, task_key_or_id: str, **kwargs: object) -> str | None:
        aid = await real_start(self, task_key_or_id, **kwargs)  # type: ignore[arg-type]
        started.append(str(task_key_or_id))
        return aid

    monkeypatch.setattr(StudyRuntime, "start_attempt", _track_start)
    rub = _artifact_rubric()
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("static:dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                        rubric=rub,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="q", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=True, reason="want hyp"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan.model_dump_json()),
    ]
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="no dangling AttemptStarted") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_hyp_journal",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"hypothesizer"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 5
    assert "HYP" not in started


@pytest.mark.asyncio
async def test_wired_report_with_adapter_support_refuses_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supports_role True for report but no CLI journal → CliRefuse, REPORT not started."""
    from coscientist.api.runtime import StudyRuntime

    real_supports = LlmAgentAdapter.supports_role

    @staticmethod
    def _support(role: str) -> bool:
        if role == "report":
            return True
        return real_supports(role)

    monkeypatch.setattr(LlmAgentAdapter, "supports_role", _support)
    started: list[str] = []
    real_start = StudyRuntime.start_attempt

    async def _track_start(self: StudyRuntime, task_key_or_id: str, **kwargs: object) -> str | None:
        aid = await real_start(self, task_key_or_id, **kwargs)  # type: ignore[arg-type]
        started.append(str(task_key_or_id))
        return aid

    monkeypatch.setattr(StudyRuntime, "start_attempt", _track_start)
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    task = tmp_path / "t.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="no dangling AttemptStarted") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_report_journal",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"report"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 5
    assert "REPORT" not in started
def test_main_unknown_profile_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--profile")
    argv[idx + 1] = "medicine"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_tool_catalog_version_pin_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--tool-catalog-version")
    argv[idx + 1] = "99"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_model_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--planner-model")
    argv[idx + 1] = "not-the-profile-model"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_run_study_loaded_profile_not_profile_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="loaded_profile must be Profile") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile="research",  # type: ignore[arg-type]
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="bad_profile",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            finalize_reserve_s=1,
            lease_ttl_s=30.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 2

