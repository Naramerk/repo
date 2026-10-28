"""Remote coder generation (slice-2): one bounded model call authoring task content."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_REMOTE_CODER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.schemas import (
    AgentError,
    OutputSpecDraft,
    RemoteCoderGenIn,
    RemoteCoderTaskSpec,
    TaskSpecDraft,
    remote_coder_task_model_for,
)
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import Budget, HitlPolicy
from coscientist.llm.client import LlmClient
from coscientist.tools.router import ToolRouter, ToolRouterConfig
from tests.agents.test_adapter_roles import CassetteTransport
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_MODEL = "test-model"


def _budget(**overrides: object) -> Budget:
    fields: dict[str, object] = {
        "max_llm_calls": 10,
        "max_tokens": 100_000,
        "max_cost_rub": None,
        "max_tool_calls": 5,
        "deadline": datetime.now(UTC) + timedelta(seconds=600),
    }
    fields.update(overrides)
    return Budget(**fields)  # type: ignore[arg-type]


async def _noop_sleep(_delay: float) -> None:
    return None


async def _noop_wait_for(awaitable: object, *, timeout_s: float) -> object:
    del timeout_s
    return await awaitable  # type: ignore[attr-defined]


def _adapter_and_transport(
    bodies: list[bytes], monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[LlmAgentAdapter, CassetteTransport]:
    if monkeypatch is not None:
        monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    transport = CassetteTransport(bodies)
    client = LlmClient(
        _config(max_retries=0),
        transport,
        clock=LlmFakeClock(),
        sleep=_noop_sleep,
        wait_for=_noop_wait_for,
        rng=random.Random(0),
    )
    adapter = LlmAgentAdapter(
        client,
        role_models={"planner": _MODEL, "executor-route": _MODEL, "tz_spec": _MODEL},
        tool_router=ToolRouter(ToolRouterConfig(servers=())),
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )
    return adapter, transport


def _adapter(bodies: list[bytes], monkeypatch: pytest.MonkeyPatch | None = None) -> LlmAgentAdapter:
    adapter, _transport = _adapter_and_transport(bodies, monkeypatch)
    return adapter


def _payload() -> RemoteCoderGenIn:
    return RemoteCoderGenIn(
        study_request="проверить гипотезу о warm-up",
        task=TaskSpecDraft(
            task_key="EXP-0",
            title="compute",
            goal="посчитать метрику",
            route="experiment",
            depends_on=(),
            addresses=("q1",),
            tools_hint=("coder",),
            expected_outputs=(
                OutputSpecDraft(
                    name="coder", media_type="application/json", schema_ref=None, required=True, rubric=None
                ),
            ),
            criteria=(),
        ),
        input_names=(),
    )


_VALID = RemoteCoderTaskSpec(
    task="напиши и запусти скрипт, который считает метрику и пишет outputs/result.json",
    outputs=({"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},),
)


@pytest.mark.asyncio
async def test_generation_returns_spec_and_counts_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter([_ok_body(content=_VALID.model_dump_json())], monkeypatch=monkeypatch)
    spec, usage = await adapter.generate_remote_coder_task(
        _payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget()
    )
    assert spec.task == _VALID.task
    # Declared ['coder'] in, exactly ['coder'] out (schema-pinned names).
    assert tuple(output.name for output in spec.outputs) == ("coder",)
    assert usage.prompt_tokens > 0
    assert adapter._llm_calls_used == 1  # audit-ok[private_access]: budget accounting contract


@pytest.mark.asyncio
async def test_generation_repairs_once_on_schema_error(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        [
            _ok_body(content='{"task": "t", "outputs": [{"name": "coder"}]}'),
            _ok_body(content=_VALID.model_dump_json()),
        ],
        monkeypatch=monkeypatch,
    )
    spec, _usage = await adapter.generate_remote_coder_task(
        _payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget()
    )
    assert spec.outputs[0].path == "outputs/result.json"


@pytest.mark.asyncio
async def test_generation_unknown_prompt_refuses() -> None:
    adapter = _adapter([])
    with pytest.raises(AgentError, match="remote coder prompt unavailable"):
        await adapter.generate_remote_coder_task(
            _payload(), prompt_ref=PromptRef(prompt_id="role.missing", version="9"), budget=_budget()
        )


@pytest.mark.asyncio
async def test_generation_exhausted_budget_refuses() -> None:
    adapter = _adapter([_ok_body(content=_VALID.model_dump_json())])
    with pytest.raises(AgentError, match="budget.max_llm_calls exceeded"):
        await adapter.generate_remote_coder_task(
            _payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget(max_llm_calls=0)
        )


@pytest.mark.asyncio
async def test_generation_schema_fail_after_repair_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter([_ok_body(content="not-json"), _ok_body(content="still-not-json")], monkeypatch=monkeypatch)
    with pytest.raises(AgentError, match="remote-coder output failed schema validation"):
        await adapter.generate_remote_coder_task(_payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget())


# --- structural anti-invention: outputs[].name pinned to the declared names ---


def _def_name_schemas(schema: dict) -> list[dict]:
    return [
        definition["properties"]["name"]
        for definition in schema["$defs"].values()
        if "name" in definition.get("properties", {})
    ]


def test_remote_coder_task_model_single_name_is_enum_not_const() -> None:
    """Single-name Literal serializes as enum, not const (provider compat; tools_hint precedent)."""
    schema = remote_coder_task_model_for(("coder",)).model_json_schema()
    (name,) = _def_name_schemas(schema)
    assert name == {"enum": ["coder"], "type": "string"}


@pytest.mark.asyncio
async def test_generation_request_schema_pins_expected_output_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The captured response_format schema enumerates exactly the declared outputs.

    Structural anti-invention (live st_idem_234ce9bd: the model invented 8
    output names and the study died at the dispatch validator) — the generation
    schema itself now forbids any undeclared name; the packaging rule with THIS
    task's names rides the system message.
    """
    adapter, transport = _adapter_and_transport(
        [_ok_body(content=_VALID.model_dump_json())], monkeypatch=monkeypatch
    )
    await adapter.generate_remote_coder_task(_payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget())
    request = transport.requests[0]
    schema = request["response_format"]["json_schema"]["schema"]
    (name,) = _def_name_schemas(schema)
    assert name == {"enum": ["coder"], "type": "string"}
    system = request["messages"][0]["content"]
    assert 'Зарегистрируй ровно эти выходы: ["coder"]' in system
    assert "все артефакты (файлы/таблицы/графики) пакуются в них" in system


@pytest.mark.asyncio
async def test_generation_request_schema_enumerates_all_declared_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = RemoteCoderGenIn(
        study_request="проверить гипотезу о warm-up",
        task=TaskSpecDraft(
            task_key="EXP-0",
            title="compute",
            goal="посчитать метрику",
            route="experiment",
            depends_on=(),
            addresses=("q1",),
            tools_hint=("coder",),
            expected_outputs=(
                OutputSpecDraft(
                    name="coder", media_type="application/json", schema_ref=None, required=True, rubric=None
                ),
                OutputSpecDraft(
                    name="figures", media_type="image/png", schema_ref=None, required=False, rubric=None
                ),
            ),
            criteria=(),
        ),
        input_names=(),
    )
    body = RemoteCoderTaskSpec(
        task="напиши и запусти скрипт",
        outputs=(
            {"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},
            {"name": "figures", "path": "outputs/figures.png", "media_type": "image/png"},
        ),
    ).model_dump_json()
    adapter, transport = _adapter_and_transport([_ok_body(content=body)], monkeypatch=monkeypatch)
    spec, _usage = await adapter.generate_remote_coder_task(
        payload, prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget()
    )
    assert tuple(output.name for output in spec.outputs) == ("coder", "figures")
    schema = transport.requests[0]["response_format"]["json_schema"]["schema"]
    (name,) = _def_name_schemas(schema)
    assert name["enum"] == ["coder", "figures"]


@pytest.mark.asyncio
async def test_generation_invented_output_name_refuses_after_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invented output name now dies at the generation schema itself.

    The name is outside the pinned Literal, so the model cannot smuggle it past
    the response schema — refused after the one bounded repair round, before
    any dispatch coverage validation.
    """
    invented = RemoteCoderTaskSpec(
        task="напиши и запусти скрипт",
        outputs=({"name": "result", "path": "outputs/result.json", "media_type": "application/json"},),
    ).model_dump_json()
    adapter, transport = _adapter_and_transport(
        [_ok_body(content=invented), _ok_body(content=invented)], monkeypatch=monkeypatch
    )
    with pytest.raises(AgentError, match="remote-coder output failed schema validation"):
        await adapter.generate_remote_coder_task(_payload(), prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget())
    assert len(transport.requests) == 2  # one bounded repair round against the pinned enum


@pytest.mark.asyncio
async def test_generation_without_declared_outputs_refuses() -> None:
    """No declared expected outputs → typed refuse (nothing to package into)."""
    adapter = _adapter([])
    payload = RemoteCoderGenIn(
        study_request="проверить гипотезу о warm-up",
        task=TaskSpecDraft(
            task_key="EXP-0",
            title="compute",
            goal="посчитать метрику",
            route="experiment",
            depends_on=(),
            addresses=("q1",),
            tools_hint=("coder",),
            expected_outputs=(),
            criteria=(),
        ),
        input_names=(),
    )
    with pytest.raises(AgentError, match="at least one declared expected output"):
        await adapter.generate_remote_coder_task(payload, prompt_ref=ROLE_REMOTE_CODER_REF, budget=_budget())
