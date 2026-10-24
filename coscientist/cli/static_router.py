"""Hermetic ``ToolRouter`` subclass for CLI cassettes (fixed payloads, no MCP I/O).

Subclass of the concrete MCP ``ToolRouter`` (#139) — not a parallel Protocol.
Production MCP wiring uses ``ToolRouter.open`` / ``attach_session``; this path only
serves ``--tools-json`` study cassettes until CLI grows a real MCP config flag.

Cassette entry shape (both keys required)::

    {"result": { ... tool output object ... }, "args": { ... call args object ... }}

``args`` may be empty ``{}`` but must be present — CLI never invents call args.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coscientist.agents.schemas import PlannedToolCall, ToolArg
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import McpServerRoute, Resolution, ToolNeed, ToolRouter, ToolRouterConfig

_STATIC_SERVER = "static"


@dataclass(frozen=True)
class _NamedCassetteTool:
    """Allow-list entry for ``LlmAgentAdapter.run`` (``NamedTool``: ``spec.name`` only)."""

    spec: ToolSpec


class StaticToolRouter(ToolRouter):
    """``ToolRouter`` with fixed name→result map; call args come from the cassette."""

    def __init__(self, *, tools: Mapping[str, Mapping[str, object]]) -> None:
        if not tools:
            raise ValueError("StaticToolRouter tools must be non-empty")
        payloads: dict[str, dict[str, object]] = {}
        call_args: dict[str, tuple[ToolArg, ...]] = {}
        for name, entry in tools.items():
            if type(name) is not str or not name.strip():
                raise ValueError("StaticToolRouter tool names must be non-empty str")
            if not isinstance(entry, Mapping):
                raise ValueError(f"StaticToolRouter tools[{name!r}] must be a mapping")
            if "result" not in entry or "args" not in entry:
                raise ValueError(
                    f"StaticToolRouter tools[{name!r}] must have required keys "
                    f"'result' and 'args' (args may be empty object)"
                )
            result = entry["result"]
            args_raw = entry["args"]
            if not isinstance(result, Mapping):
                raise ValueError(f"StaticToolRouter tools[{name!r}].result must be a mapping")
            if not isinstance(args_raw, Mapping):
                raise ValueError(f"StaticToolRouter tools[{name!r}].args must be a mapping")
            payloads[name] = dict(result)
            planned: list[ToolArg] = []
            for key, value in args_raw.items():
                if type(key) is not str or not key.strip():
                    raise ValueError(f"StaticToolRouter tools[{name!r}].args keys must be non-empty str")
                planned.append(
                    ToolArg(
                        key=key,
                        value_json=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    )
                )
            call_args[name] = tuple(planned)
        profiles = {
            name: McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)
            for name in payloads
        }
        super().__init__(
            ToolRouterConfig(
                servers=(McpServerRoute(server_name=_STATIC_SERVER, tools=profiles),)
            )
        )
        self._payloads = payloads
        self._call_args = call_args
        self.mark_open()

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._payloads)

    def named_tools(self) -> list[_NamedCassetteTool]:
        """Allow-list for ``AgentPort.run`` — ``spec.name`` from the cassette."""
        return [_NamedCassetteTool(spec=ToolSpec(name=n)) for n in sorted(self._payloads)]

    def planned_calls(self, hints: Sequence[str]) -> tuple[PlannedToolCall, ...]:
        """``PlannedToolCall`` per hint using cassette-declared args (no invented defaults)."""
        out: list[PlannedToolCall] = []
        for hint in hints:
            if hint not in self._call_args:
                raise KeyError(hint)
            out.append(PlannedToolCall(tool_name=hint, args=self._call_args[hint]))
        return tuple(out)

    def resolve(self, need: ToolNeed) -> Resolution:
        missing = Resolution(status="tool_missing", server_name=None, tool_name=None)
        if need.kind != "by_name":
            return missing
        value = need.value
        prefix = f"{_STATIC_SERVER}:"
        if value.startswith(prefix):
            tool_name = value[len(prefix) :]
        else:
            tool_name = value
        if tool_name in self._payloads:
            return Resolution(
                status="tool_found",
                server_name=_STATIC_SERVER,
                tool_name=tool_name,
            )
        return missing

    async def call(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, object],
        ctx: CallCtx,
    ) -> ToolOutput:
        del ctx
        if server_name != _STATIC_SERVER:
            raise KeyError(f"unknown server {server_name!r}")
        if tool_name not in self._payloads:
            raise KeyError(tool_name)
        declared = {a.key: json.loads(a.value_json) for a in self._call_args[tool_name]}
        if args != declared:
            raise ValueError(
                f"StaticToolRouter refuses args mismatch for {tool_name!r} "
                f"(server={server_name!r}): got {args!r}, cassette declared {declared!r}"
            )
        return ToolOutput(data=dict(self._payloads[tool_name]))
