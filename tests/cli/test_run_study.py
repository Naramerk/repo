"""CLI ``run_study`` — honest journal cycle; usage written; negatives refuse."""

from __future__ import annotations

import argparse
import io
import json
import random
from pathlib import Path
from typing import Any

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
from coscientist.core.execution.domain import Need
from coscientist.llm.client import LlmClient
from coscientist.llm.errors import LlmTransportError
from coscientist.tools.router import ToolNeed
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


def _adapter(client: LlmClient, router: FakeToolRouter | StaticToolRouter) -> LlmAgentAdapter:
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
        clock=FakeClock(),
    )


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    llm_clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
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


def _default_tools() -> dict[str, dict[str, bool]]:
    return {
        "dataset_overview": {"ok": True},
        "chemical_space_clustering": {"ok": True},
    }


async def _run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    require_hypotheses: bool = False,
    router: FakeToolRouter | StaticToolRouter | None = None,
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
) -> tuple[int, dict[str, Any], FakeToolRouter | StaticToolRouter]:
    task = tmp_path / "two_tasks.txt"
    task.write_text(task_text, encoding="utf-8")
    if router is None:
        r: FakeToolRouter | StaticToolRouter = FakeToolRouter(tools=_default_tools())
    else:
        r = router
    if isinstance(r, StaticToolRouter):
        allowed = r.tool_names
    else:
        allowed = frozenset(r.tools)
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), r)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=deadline_s,
        catalog_version=catalog_version,
        catalog=minimal_test_catalog(),
        profile="research",
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_id=study_id,
        require_hypotheses=require_hypotheses,
        finalize_reserve_s=finalize_reserve_s,
        lease_ttl_s=lease_ttl_s,
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_tool_calls=max_tool_calls,
        max_ticks=max_ticks,
        allowed_tool_names=allowed,
        study_clock=FakeClock(),
    )
    if out.getvalue().strip():
        summary: dict[str, Any] = json.loads(out.getvalue())
    else:
        summary = {}
    return code, summary, r


@pytest.mark.asyncio
async def test_run_study_fulfilled_records_executor_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert isinstance(router, FakeToolRouter)
    assert [name for name, _args in router.calls] == [
        "dataset_overview",
        "chemical_space_clustering",
    ]


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
                tools_hint=("dataset_overview",),
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
    ],
    ids=["deadline_s", "study_id", "finalize_reserve_s", "lease_ttl_s", "max_ticks"],
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
    adapter = _adapter(_client([], monkeypatch=monkeypatch), FakeToolRouter(tools={}))
    extras: dict[str, object] = {"catalog_version": "1"}
    with pytest.raises(CliRefuse, match="catalog_version must be int") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
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
            allowed_tool_names=frozenset(),
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
                tools_hint=("dataset_overview",),
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
                tools_hint=("dataset_overview",),
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
                        rubric=rub,
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
async def test_executor_route_bad_schema_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_plan_out_json(), prompt_tokens=10, completion_tokens=20),
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
        _ok_body(content=_plan_out_json()),
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
    with pytest.raises(CliRefuse, match="not in router allow-list") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 3


def test_main_missing_required_args_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["run", "missing_task.txt"])
    assert ei.value.code != 0


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
    router = StaticToolRouter(tools={"t1": {"ok": True}})
    assert router.tool_names == frozenset({"t1"})
    assert router.resolve(ToolNeed(kind="by_name", value="t1")).status == "tool_found"
    assert router.resolve(ToolNeed(kind="by_name", value="missing")).status == "tool_missing"


@pytest.mark.asyncio
async def test_static_tool_router_call_empty_args_ok() -> None:
    router = StaticToolRouter(tools={"t1": {"ok": True}})
    result = await router.call("t1", {}, study_id="s", task_key="k", attempt_id="a")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_static_tool_router_call_nonempty_args_refuses() -> None:
    router = StaticToolRouter(tools={"t1": {"ok": True}})
    with pytest.raises(ValueError, match="refuses non-empty args"):
        await router.call("t1", {"x": 1}, study_id="s", task_key="k", attempt_id="a")


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
    with pytest.raises(LlmTransportError) as ei:
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
    with pytest.raises(LlmTransportError) as ei:
        await transport.request("POST", "not-a-url", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is False


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
