"""CLI tool surface: cassette ``StaticToolRouter`` or live MCP ``ToolRouter``.

Both paths expose the same allow-list / ``planned_calls`` contract for ``run_study``.
Live args come only from explicit ``--tool-call-args`` (exact ``server:tool`` keys);
unknown tools refuse — no blanket-allow, no invented defaults.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coscientist.agents.schemas import PlannedToolCall, ToolArg
from coscientist.tools.contract import ToolSpec
from coscientist.tools.router import ToolRouter, ToolRouterError


@dataclass(frozen=True)
class _NamedBoundTool:
    """Allow-list entry for ``LlmAgentAdapter.run`` (``NamedTool``: ``spec.name`` only)."""

    spec: ToolSpec


class LiveCliToolSurface:
    """Opened MCP ``ToolRouter`` + explicit call-args map (exact ``server:tool`` keys)."""

    def __init__(
        self,
        router: ToolRouter,
        *,
        call_args: Mapping[str, Mapping[str, object]],
    ) -> None:
        if not isinstance(router, ToolRouter):
            raise TypeError(f"router must be ToolRouter, got {type(router).__name__}")
        try:
            bound = _bound_spec_names(router)
        except ToolRouterError as exc:
            raise ValueError(f"LiveCliToolSurface requires an open ToolRouter: {exc}") from exc
        if not bound:
            raise ValueError("LiveCliToolSurface requires at least one bound tool")
        parsed: dict[str, tuple[ToolArg, ...]] = {}
        for name, args_raw in call_args.items():
            if type(name) is not str or not name.strip():
                raise ValueError("tool-call-args keys must be non-empty str")
            if name not in bound:
                raise ValueError(
                    f"tool-call-args key {name!r} is not a bound tool "
                    f"(bound={sorted(bound)})"
                )
            if not isinstance(args_raw, Mapping):
                raise ValueError(f"tool-call-args[{name!r}] must be a JSON object")
            planned: list[ToolArg] = []
            for key, value in args_raw.items():
                if type(key) is not str or not key.strip():
                    raise ValueError(f"tool-call-args[{name!r}] keys must be non-empty str")
                planned.append(
                    ToolArg(
                        key=key,
                        value_json=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    )
                )
            parsed[name] = tuple(planned)
        self._router = router
        self._bound = bound
        self._call_args = parsed

    @property
    def router(self) -> ToolRouter:
        return self._router

    @property
    def tool_names(self) -> frozenset[str]:
        """Exact ``server:tool`` names bound on the open router."""
        return self._bound

    def named_tools(self) -> list[_NamedBoundTool]:
        """Allow-list for ``AgentPort.run`` — ``spec.name`` is ``server:tool``."""
        return [_NamedBoundTool(spec=ToolSpec(name=n)) for n in sorted(self._bound)]

    def planned_calls(self, hints: Sequence[str]) -> tuple[PlannedToolCall, ...]:
        """``PlannedToolCall`` per hint using declared args (no invented defaults)."""
        out: list[PlannedToolCall] = []
        for hint in hints:
            if hint not in self._bound:
                raise KeyError(hint)
            if hint not in self._call_args:
                raise KeyError(hint)
            out.append(PlannedToolCall(tool_name=hint, args=self._call_args[hint]))
        return tuple(out)


def _bound_spec_names(router: ToolRouter) -> frozenset[str]:
    """Public bound ``ToolSpec.name`` set via ``require_tool`` (no private ``_tools``)."""
    names: set[str] = set()
    for route in router.config.servers:
        for tool_name in route.tools:
            bound = router.require_tool(route.server_name, tool_name)
            names.add(bound.spec.name)
    return frozenset(names)


__all__ = ["LiveCliToolSurface"]
