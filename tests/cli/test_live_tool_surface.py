"""Live MCP CLI tool surface + open_tool_router_for_cli (exact server:tool)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coscientist.cli.tool_surface import LiveCliToolSurface
from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.mcp_live import (
    McpToolRouterBundle,
    open_tool_router_for_cli,
)
from coscientist.tools.router import McpServerRoute, ToolNeed, ToolRouterConfig
from tests.mcp.conftest import SystemClock, make_config, system_sleep, system_wait_for

ECHO_STDIO = Path(__file__).resolve().parents[1] / "mcp" / "echo_stdio_server.py"
_SERVER = "echo-srv"


def _profile() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _echo_bundle() -> McpToolRouterBundle:
    return McpToolRouterBundle(
        client=make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=_SERVER,
                    command=sys.executable,
                    args=("-u", str(ECHO_STDIO)),
                    env_keys=(),
                ),
            )
        ),
        router=ToolRouterConfig(
            servers=(McpServerRoute(server_name=_SERVER, tools={"echo": _profile()}),)
        ),
    )


def _ctx() -> CallCtx:
    return CallCtx(
        study_id="st",
        task_key="T0",
        attempt_id="att#1",
        hitl_policy=HitlPolicy(mode="headless"),
    )


@pytest.mark.asyncio
async def test_open_tool_router_for_cli_stdio_and_surface() -> None:
    _client, router = await open_tool_router_for_cli(
        _echo_bundle(),
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
    )
    try:
        bound = f"{_SERVER}:echo"
        surface = LiveCliToolSurface(
            router,
            call_args={bound: {"text": "cli-live"}},
        )
        assert surface.tool_names == frozenset({bound})
        assert [t.spec.name for t in surface.named_tools()] == [bound]
        planned = surface.planned_calls((bound,))
        assert len(planned) == 1
        assert planned[0].tool_name == bound
        assert json.loads(planned[0].args[0].value_json) == "cli-live"
        resolution = router.resolve(ToolNeed(kind="by_name", value=bound))
        assert resolution.status == "tool_found"
        out = await router.call(
            _SERVER,
            "echo",
            {"text": "cli-live"},
            _ctx(),
        )
        assert out.data["result"]["content"][0]["text"] == "cli-live"
        with pytest.raises(KeyError):
            surface.planned_calls(("missing:tool",))
        with pytest.raises(ValueError, match="not a bound tool"):
            LiveCliToolSurface(router, call_args={"other:tool": {}})
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_live_surface_unknown_hint_keyerror() -> None:
    _client, router = await open_tool_router_for_cli(
        _echo_bundle(),
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
    )
    try:
        bound = f"{_SERVER}:echo"
        surface = LiveCliToolSurface(router, call_args={bound: {}})
        with pytest.raises(KeyError):
            surface.planned_calls((bound, f"{_SERVER}:missing"))
    finally:
        await router.aclose()


def test_live_surface_allow_list_is_call_args_not_all_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound MCP tools without --tool-call-args must not enter the CLI allow-list."""
    declared = f"{_SERVER}:echo"
    extra_bound = f"{_SERVER}:other"
    monkeypatch.setattr(
        "coscientist.cli.tool_surface._bound_spec_names",
        lambda _router: frozenset({declared, extra_bound}),
    )
    router = ToolRouter(
        ToolRouterConfig(
            servers=(McpServerRoute(server_name=_SERVER, tools={"echo": _profile()}),)
        )
    )
    router.mark_open()
    surface = LiveCliToolSurface(router, call_args={declared: {"text": "only-echo"}})
    assert surface.tool_names == frozenset({declared})
    assert [t.spec.name for t in surface.named_tools()] == [declared]
    with pytest.raises(KeyError):
        surface.planned_calls((extra_bound,))
    planned = surface.planned_calls((declared,))
    assert len(planned) == 1
    assert planned[0].tool_name == declared
