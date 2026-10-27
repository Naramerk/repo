"""CLI tool surface: cassette ``StaticToolRouter`` or live MCP ``ToolRouter``.

Both paths expose the same allow-list / ``planned_calls`` contract for ``run_study``.
Live args come only from explicit ``--tool-call-args`` (exact ``server:tool`` keys);
the public allow-list is that declared-args set (not every MCP-bound tool) so
``tools_hint`` cannot pass allow-list then ``KeyError`` in ``planned_calls``.
Unknown tools refuse — no blanket-allow, no invented defaults.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coscientist.agents.schemas import PlannedToolCall, ToolArg
from coscientist.core.execution.tool_loader import ensure_core_tools_ready
from coscientist.core.execution.tool_registry import RegisteredToolSpec
from coscientist.tools.contract import ToolSpec
from coscientist.tools.router import CODER_NEED_VALUE, ToolRouter, ToolRouterConfig, ToolRouterError


def register_trusted_mcp_tools(config: ToolRouterConfig) -> None:
    """Load only pinned configuration declarations, before the registry freezes."""
    ensure_core_tools_ready(
        tuple(
            RegisteredToolSpec(
                name=f"{route.server_name}:{name}",
                provider="mcp",
                effect=profile.effect,
                is_fetch=profile.is_fetch,
                idempotent=profile.idempotent,
                max_retries=profile.max_retries,
            )
            for route in config.servers
            for name, profile in route.tools.items()
        )
    )


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
        call_args: Mapping[str, Mapping[str, object]] | None,
        allowed_tool_ids: Sequence[str] | None = None,
    ) -> None:
        if not isinstance(router, ToolRouter):
            raise TypeError(f"router must be ToolRouter, got {type(router).__name__}")
        try:
            bound = _bound_spec_names(router)
        except ToolRouterError as exc:
            raise ValueError(f"LiveCliToolSurface requires an open ToolRouter: {exc}") from exc
        if not bound and allowed_tool_ids != [] and allowed_tool_ids != ():
            raise ValueError("LiveCliToolSurface requires at least one bound tool")
        if allowed_tool_ids is None:
            permitted = bound
        else:
            permitted = frozenset(allowed_tool_ids)
        if permitted - bound:
            raise ValueError("allowed_tool_ids contains unknown bindings")
        self.automatic = call_args is None
        self._permitted = permitted
        parsed: dict[str, tuple[ToolArg, ...]] = {}
        if call_args is not None:
            for name, args_raw in call_args.items():
                if type(name) is not str or not name.strip():
                    raise ValueError("tool-call-args keys must be non-empty str")
                if name == CODER_NEED_VALUE:
                    # Reserved coder binding: only on an attached coder route, and it
                    # carries no agent-declared args (CoderTool checks ids at dispatch).
                    if router.coder_route is None:
                        raise ValueError(
                            "tool-call-args key 'coder' requires an attached coder route (--coder-backend)"
                        )
                    if args_raw:
                        raise ValueError("tool-call-args['coder'] must be an empty object")
                    parsed[name] = ()
                    continue
                if name not in permitted:
                    raise ValueError(f"tool-call-args key {name!r} is not a bound tool (bound={sorted(bound)})")
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
        # `bound` validated call_args ⊆ MCP bindings; allow-list is call_args only.
        self._router = router
        self._call_args = parsed

    @property
    def router(self) -> ToolRouter:
        return self._router

    @property
    def tool_names(self) -> frozenset[str]:
        """Exact ``server:tool`` names with declared ``--tool-call-args`` (CLI allow-list).

        Bound MCP tools without call-args are intentionally excluded — same invariant as
        ``StaticToolRouter`` (every allow-listed name is ``planned_calls``-satisfiable).
        The reserved ``coder`` key (empty args, attached coder route only) is included
        when declared.
        """
        if self.automatic:
            return self._permitted
        return frozenset(self._call_args)

    def named_tools(self) -> list[_NamedBoundTool]:
        """Allow-list for ``AgentPort.run`` — ``spec.name`` is ``server:tool``."""
        tools: list[_NamedBoundTool] = []
        for name in sorted(self.tool_names):
            if name == CODER_NEED_VALUE:
                tools.append(_NamedBoundTool(spec=ToolSpec(name=name)))
            else:
                server, tool = name.split(":", 1)  # audit-ok[str_split]: server:tool id from trusted bundle
                tools.append(_NamedBoundTool(spec=self._router.require_tool(server, tool).spec))
        return tools

    def planned_calls(self, hints: Sequence[str]) -> tuple[PlannedToolCall, ...]:
        """``PlannedToolCall`` per hint using declared args (no invented defaults)."""
        out: list[PlannedToolCall] = []
        for hint in hints:
            if self.automatic:
                if hint not in self._permitted:
                    raise KeyError(hint)
                continue
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
