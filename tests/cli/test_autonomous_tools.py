"""Offline real CLI/runtime/MCP boundary tests; no research providers are called."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coscientist.agents.schemas import AgentError, QuestionIntent, TaskIntent
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import run_study
from coscientist.cli.tool_execution import validate_tool_arguments
from coscientist.cli.tool_surface import LiveCliToolSurface, register_trusted_mcp_tools
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy
from coscientist.llm.types import LlmResponse, Usage
from tests.agents.test_adapter_roles import _SERVER, _bound_router
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import _RESEARCH_PROFILE, _adapter, _intent_json, _read_events, _spec_json


class _ScriptedClient:
    def __init__(
        self, name: str, *, invalid: bool = False, selection: str = "last", recipe=False, researcher=False
    ) -> None:
        self.name = name
        self.receipts: list[dict] = []
        self.invalid = invalid
        self.selection = selection
        self.recipe = recipe
        self.researcher = researcher
        self.contexts: list[dict] = []
        self.by_attempt: dict[str, list[dict]] = {}

    async def complete(self, request):
        schema = request.response_format.name
        if schema == "tz_spec":
            result = json.loads(_spec_json())
            result["object"] = "synthetic paginated sensor"
            result["goal"]["statement"] = "retrieve two sensor pages"
        elif schema == "planner":
            result = json.loads(
                _intent_json(
                    (
                        TaskIntent(
                            key="EXP-A",
                            goal="retrieve both pages",
                            route="experiment",
                            question_indices=(1,),
                            tools_hint=(self.name,) if self.recipe else (),
                            output_names=(self.name if self.recipe else "sensor-pages",),
                        ),
                    ),
                    questions=(QuestionIntent(text="Return the recorded sensor pages", final_type="artifact"),),
                )
            )
            if self.researcher:
                result["literature"] = {"kind": "need", "needed": True, "reason": "search the requested literature"}
        else:
            assert schema == "tool_action"
            context = json.loads(request.messages[1].content)
            if context["tools"]:
                assert context["tools"][0]["description"] == "Read sensor page; use returned next cursor"
                assert context["tools"][0]["input_schema"]["properties"]["cursor"] == {"type": "integer"}
            self.contexts.append(context)
            receipts = self.by_attempt.setdefault(context["attempt_id"], [])
            if request.messages[-1].role == "user" and '"call_id"' in request.messages[-1].content:
                receipt = json.loads(request.messages[-1].content)
                self.receipts.append(receipt)
                receipts.append(receipt)
            if len(receipts) < 2:
                cursor = (
                    0
                    if not receipts
                    else json.loads(receipts[-1]["result_json"])["result"]["structuredContent"]["next"]
                )
                result = {
                    "action": "call",
                    "call": {
                        "tool_name": self.name,
                        "args": [{"key": "cursor", "value_json": json.dumps("bad" if self.invalid else cursor)}],
                    },
                    "output_json": None,
                    "selections": [],
                }
                if self.invalid == "duplicate":
                    result["call"]["args"] *= 2
            else:
                result = {
                    "action": "finish",
                    "call": None,
                    "output_json": json.dumps(
                        {"summary": "Both actual sensor pages retrieved", "values": [], "tool_found": [self.name]}
                    ),
                    "selections": [{"output_name": "sensor-pages", "call_id": receipts[-1]["call_id"]}],
                }
                if self.selection == "missing":
                    result["selections"] = []
                elif self.selection == "unknown":
                    result["selections"][0]["call_id"] = "invented-call"
                elif self.selection == "first":
                    result["selections"][0]["call_id"] = receipts[0]["call_id"]
                if context["task"]["route"] == "literature":
                    result["output_json"] = json.dumps({"papers": []})
                    result["selections"] = []
        return LlmResponse(
            content=json.dumps(result),
            model=request.model,
            finish_reason="stop",
            usage=Usage(prompt_tokens=1, completion_tokens=1, latency_ms=0),
        )


async def _run(
    tmp_path: Path,
    *,
    invalid: bool = False,
    allowed=None,
    tool_error=False,
    selection="last",
    max_tool_calls=4,
    recipe=False,
    researcher=False,
):
    calls: list[dict] = []

    def handler(method, params):
        if method == "initialize":
            return {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sensor", "version": "1"},
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "probe",
                        "description": "Read sensor page; use returned next cursor",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"cursor": {"type": "integer"}},
                            "required": ["cursor"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        assert method == "tools/call"
        calls.append(params["arguments"])
        cursor = params["arguments"]["cursor"]
        return {
            "content": [],
            "structuredContent": {"sample": cursor, "next": cursor + 1},
            "isError": tool_error and cursor == 0,
        }

    name = f"{_SERVER}:probe"
    async with _bound_router(handler, tool="probe") as router:
        register_trusted_mcp_tools(router.config)
        surface = LiveCliToolSurface(
            router, call_args={name: {"cursor": 0}} if recipe else None, allowed_tool_ids=allowed
        )
        client = _ScriptedClient(name, invalid=invalid, selection=selection, recipe=recipe, researcher=researcher)
        adapter = _adapter(client, router)
        profile = _RESEARCH_PROFILE
        if researcher:
            from tests.cli.test_hyp_lit_wiring import _adapter as literature_adapter

            adapter = literature_adapter(client, router)
            profile = profile.model_copy(
                update={
                    "roles": {
                        **profile.roles,
                        "researcher": profile.roles["researcher"].model_copy(update={"tools": ("probe",)}),
                    }
                }
            )
        task_path = tmp_path / "request.txt"
        task_path.write_text("Retrieve two sensor pages using the returned cursor.")
        sink = io.StringIO()
        try:
            result = await run_study(
                task_path=task_path,
                data_root=tmp_path / "data",
                deadline_s=3600,
                catalog_version=2,
                catalog=minimal_test_catalog(),
                loaded_profile=profile,
                owner="test",
                adapter=adapter,
                out=sink,
                study_id="sensor",
                require_hypotheses=False,
                confirmations_needed=1,
                refutations_needed=1,
                refutation_priority=False,
                supported_needed=2,
                max_expansions=2,
                max_attempts=1,
                finalize_reserve_s=30,
                lease_ttl_s=10800,
                max_llm_calls=20,
                max_tokens=10000,
                max_tool_calls=max_tool_calls,
                max_ticks=20,
                tools=surface,
                hitl_policy=HitlPolicy(mode="headless"),
                study_clock=FakeClock(),
                wired_roles=frozenset({"researcher"}) if researcher else frozenset(),
            )
        except CliRefuse as exc:
            assert exc.code in (4, 7), exc
            result = exc.code
        events = await _read_events(tmp_path / "data", "sensor")
    if researcher:
        assert any(context["task"]["route"] == "literature" for context in client.contexts)
        assert all(context["request"] == task_path.read_text() for context in client.contexts)
    return result, calls, client.receipts, events


@pytest.mark.asyncio
async def test_real_cli_empty_hints_dependent_calls_retained(tmp_path):
    _, calls, receipts, events = await _run(tmp_path)
    assert calls == [{"cursor": 0}, {"cursor": 1}]
    assert len({receipt["call_id"] for receipt in receipts}) == 2
    assert len({receipt["artifact_id"] for receipt in receipts}) == 2
    assert len([event for event in events if event.type == "ToolCalled"]) == 2
    registered = {event.payload["artifact_id"] for event in events if event.type == "ArtifactRegistered"}
    assert {receipt["artifact_id"] for receipt in receipts} <= registered
    result = next(
        event for event in events if event.type == "ResultRecorded" and event.payload["task_id"].endswith("EXP-A")
    )
    assert len(result.payload["output_map"]) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid,allowed", [(True, None), ("duplicate", None), (False, [])])
async def test_invalid_or_disabled_never_dispatch(tmp_path, invalid, allowed):
    _, calls, _, events = await _run(tmp_path, invalid=invalid, allowed=allowed)
    assert calls == []
    assert not any(event.type == "ToolCalled" for event in events)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://invalid.test/schema"},
        {"$defs": {"unused": {"$ref": "file:///etc/passwd"}}},
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}},
    ],
)
def test_schema_refusal_local(schema):
    with pytest.raises(AgentError):
        validate_tool_arguments(schema, {})


@pytest.mark.asyncio
async def test_failed_response_retained_then_success_selected(tmp_path):
    _, calls, receipts, events = await _run(tmp_path, tool_error=True)
    assert len(calls) == 2
    assert [receipt["outcome"] for receipt in receipts] == ["error", "ok"]
    assert [event.payload["outcome"] for event in events if event.type == "ToolCalled"] == ["error", "ok"]
    assert all(receipt["artifact_id"] for receipt in receipts)


@pytest.mark.asyncio
async def test_fixed_recipe_failed_receipt_never_satisfies_required_output(tmp_path):
    code, calls, _, events = await _run(tmp_path, recipe=True, tool_error=True)
    assert code != 0
    assert calls == [{"cursor": 0}]
    assert any(event.type == "ToolCalled" and event.payload["outcome"] == "error" for event in events)
    assert not any(
        event.type == "ResultRecorded"
        and event.payload["task_id"].endswith("EXP-A")
        and event.payload["computed_outcome"] == "SUCCESS"
        for event in events
    )


@pytest.mark.asyncio
async def test_researcher_same_loop_receives_actual_study_request(tmp_path):
    _, calls, receipts, events = await _run(tmp_path, researcher=True)
    assert len(calls) == 4
    assert len(receipts) == 4
    assert any(event.type == "ResultRecorded" and event.payload["task_id"].endswith("LIT") for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection,tool_error", [("missing", False), ("unknown", False), ("first", True)])
async def test_finish_cannot_invent_or_use_failed_required_output(tmp_path, selection, tool_error):
    code, _, _, events = await _run(tmp_path, selection=selection, tool_error=tool_error)
    assert code != 0
    assert not any(
        event.type == "ResultRecorded"
        and event.payload["task_id"].endswith("EXP-A")
        and event.payload["computed_outcome"] == "SUCCESS"
        for event in events
    )


@pytest.mark.asyncio
async def test_tool_budget_prevents_second_rpc(tmp_path):
    code, calls, _, _ = await _run(tmp_path, max_tool_calls=1)
    assert code == 4
    assert calls == [{"cursor": 0}]


@pytest.mark.asyncio
async def test_frozen_registry_same_process_subset_and_mutation(tmp_path):
    from coscientist.core.execution.tool_loader import ensure_core_tools_ready
    from coscientist.core.execution.tool_registry import RegisteredToolSpec, get_registered, is_registry_frozen
    from tests.helpers import close_runtime, open_runtime

    first = RegisteredToolSpec(name="sensor:probe", effect="read", provider="mcp")
    second = RegisteredToolSpec(name="sensor:read", effect="read", provider="mcp")
    ensure_core_tools_ready((first, second))
    first_runtime = await open_runtime(tmp_path, "first", owner="test")
    try:
        ensure_core_tools_ready((first,))
        second_runtime = await open_runtime(tmp_path, "second", owner="test")
        await close_runtime(second_runtime)
    finally:
        await close_runtime(first_runtime)
    assert is_registry_frozen()
    with pytest.raises(ValueError, match="restart"):
        ensure_core_tools_ready((first.model_copy(update={"effect": "write"}),))
    with pytest.raises(ValueError, match="restart"):
        ensure_core_tools_ready((RegisteredToolSpec(name="foreign:new", effect="read"),))
    assert get_registered(first.name) == first


@pytest.mark.parametrize("names", [("",), ("  ",), (" padded",), ("duplicate", "duplicate")])
def test_invalid_output_identities_refuse(names):
    with pytest.raises(ValueError, match="output_names"):
        TaskIntent(goal="Retrieve sensor pages", route="experiment", output_names=names)


def test_explicit_output_contract_survives_continue_roundtrip():
    from coscientist.agents.derive import derive_plan
    from coscientist.agents.schemas import PlanIntent
    from coscientist.cli.continue_cmd import draft_from_task_spec, spec_redrivable
    from coscientist.cli.run import _to_task_spec
    from coscientist.core.execution.domain import Need

    intent = PlanIntent(
        tasks=(TaskIntent(goal="retrieve", route="experiment", question_indices=(1,), output_names=("sensor-pages",)),),
        questions=(QuestionIntent(text="Return pages", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=False, reason="not needed"),
        literature=Need(kind="need", needed=False, reason="not needed"),
    )
    draft = derive_plan(intent, catalog_templates=(), tool_ids=()).tasks[0]
    spec = _to_task_spec(draft)
    assert spec_redrivable(spec)
    assert draft_from_task_spec(spec) == draft
    assert spec.expected_outputs[0].name == "sensor-pages"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_stop_after_observation_prevents_next_rpc(tmp_path, monkeypatch, stop):
    from datetime import timedelta

    from coscientist.cli.tool_execution import RuntimeMcpExecutor

    original = RuntimeMcpExecutor.invoke

    async def invoke_then_stop(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if stop == "cancel":
            await self.runtime.cancel_study()
        else:
            self.runtime.clock.advance(timedelta(hours=2))
        return result

    monkeypatch.setattr(RuntimeMcpExecutor, "invoke", invoke_then_stop)
    code, calls, receipts, events = await _run(tmp_path)
    assert code != 0
    assert calls == [{"cursor": 0}]
    assert receipts == []
    assert len([event for event in events if event.type == "ToolCalled"]) == 1


@pytest.mark.asyncio
async def test_empty_mask_skips_all_mcp_connections(tmp_path, monkeypatch):
    import argparse

    from coscientist.cli.run import _open_tool_surface
    from tests.cli.test_live_tool_surface import _echo_bundle

    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled MCP server must not be connected")

    monkeypatch.setattr("coscientist.cli.run.open_tool_router_for_cli", forbidden)
    path = tmp_path / "trusted.json"
    path.write_text(_echo_bundle().model_dump_json())
    surface, router = await _open_tool_surface(
        argparse.Namespace(
            tools_json=None,
            mcp_tool_bundle=path,
            tool_call_args=None,
            coder_backend=None,
            allowed_tool_ids=[],
        )
    )
    try:
        assert surface.tool_names == frozenset()
        assert not router.config.servers
    finally:
        await router.aclose()
