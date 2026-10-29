"""CLI profile load, tool-surface open, and task-input helpers (split from run)."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.prompts.roles import (
    ROLE_HYPOTHESIZER_REF,
    ROLE_RESEARCHER_REF,
)
from coscientist.agents.schemas import PlannedToolCall
from coscientist.cli.errors import CliRefuse
from coscientist.cli.llm_clock import wall_sleep, wall_wait_for
from coscientist.cli.static_router import StaticToolRouter
from coscientist.cli.tool_surface import LiveCliToolSurface, register_trusted_mcp_tools
from coscientist.core.execution.input_limits import UserInput
from coscientist.profiles.errors import (
    CatalogVersionError,
    ProfileLoadError,
    UnknownProfileError,
)
from coscientist.profiles.load import load_profile
from coscientist.profiles.models import Profile
from coscientist.tools.mcp_live import (
    McpLiveError,
    load_mcp_tool_router_bundle,
    open_tool_router_for_cli,
)
from coscientist.tools.router import ToolRouter, ToolRouterError

_OPTIONAL_ROLES = frozenset({"hypothesizer", "researcher", "report", "critic", "reviewer"})
_ROLE_HYP = "hypothesizer"
_ROLE_LIT = "researcher"
_ROLE_REPORT = "report"
_ROLE_CRITIC = "critic"
_ROLE_REVIEWER = "reviewer"
# CLI live/cassette path roles/routes from packaged production research.v1 (DESIGN §7.1).
_CLI_REQUIRED_ROLES = ("tz_spec", "planner", "executor-route")
# Optional adapter roles wired through LlmAgentAdapter when present in the
# profile (critic: plan-quality gate; reviewer: post-close review lane).
_CLI_ADAPTER_OPTIONAL_ROLES = (_ROLE_CRITIC, _ROLE_REVIEWER)
_CLI_REQUIRED_ROUTES: dict[str, str] = {
    "spec": "tz_spec",
    "plan": "planner",
    "execute": "executor-route",
}
# Wired optional need stages (DESIGN §7.1): profile route key → wired role.
# --wired-roles hypothesizer/researcher requires the profile route to exist and
# wire exactly that role (same fail-closed rule as _CLI_REQUIRED_ROUTES).
_WIRED_STAGE_ROUTES: dict[str, str] = {_ROLE_HYP: "hypotheses", _ROLE_LIT: "literature"}
_WIRED_ROLE_PROMPTS: dict[str, PromptRef] = {
    _ROLE_HYP: ROLE_HYPOTHESIZER_REF,
    _ROLE_LIT: ROLE_RESEARCHER_REF,
}
# Fixed suffix → media-type map for --task-input files. Deterministic from the
# file name only (content is never sniffed); unknown suffixes keep the
# UserInput default `application/octet-stream` so intake limits still apply.
_TASK_INPUT_MEDIA_TYPES: dict[str, str] = {
    ".csv": "text/csv",
    ".tsv": "text/tsv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
}


class _NamedToolSpec(Protocol):
    @property
    def name(self) -> str: ...


class _NamedTool(Protocol):
    @property
    def spec(self) -> _NamedToolSpec: ...


class CliToolSurface(Protocol):
    """Cassette or live MCP surface used by ``run_study`` (exact allow-list)."""

    @property
    def tool_names(self) -> frozenset[str]: ...

    def named_tools(self) -> Sequence[_NamedTool]: ...

    def planned_calls(self, hints: Sequence[str]) -> tuple[PlannedToolCall, ...]: ...


def _task_input_media_type(path: Path) -> str:
    media_type = _TASK_INPUT_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is not None:
        return media_type
    return "application/octet-stream"


class _McpMonoClock:
    """Monotonic clock for ``open_tool_router_for_cli`` (MCP runtime Protocol)."""

    def monotonic(self) -> float:
        return time.monotonic()


def _load_cli_profile(
    name: str,
    *,
    expected_tool_catalog_version: int,
) -> Profile:
    """Fail-closed packaged production profile load (unknown / pin mismatch → CliRefuse)."""
    try:
        return load_profile(
            name,
            expected_tool_catalog_version=expected_tool_catalog_version,
        )
    except UnknownProfileError as exc:
        raise CliRefuse(str(exc), code=2) from exc
    except CatalogVersionError as exc:
        raise CliRefuse(str(exc), code=2) from exc
    except ProfileLoadError as exc:
        raise CliRefuse(str(exc), code=2) from exc


def _role_models_from_profile(profile: Profile) -> dict[str, str]:
    """Required CLI roles + optional adapter roles when the profile carries them.

    Optional entries (critic / reviewer) are inert until the driver runs the
    role (gated on --wired-roles); a profile without them keeps the adapter at
    the three required roles and the wired stage refuses (code 5).
    """
    missing = [r for r in _CLI_REQUIRED_ROLES if r not in profile.roles]
    if missing:
        raise CliRefuse(
            f"profile {profile.name!r} missing required roles for CLI path: {missing!r}",
            code=2,
        )
    models = {role: profile.roles[role].model for role in _CLI_REQUIRED_ROLES}
    for role in _CLI_ADAPTER_OPTIONAL_ROLES:
        cfg = profile.roles.get(role)
        if cfg is not None:
            models[role] = cfg.model
    return models


def _wired_role_adapters(profile: Profile, wired_roles: frozenset[str]) -> tuple[dict[str, str], dict[str, PromptRef]]:
    """Profile-backed model + prompt entries for the wired need-stage roles.

    Fail-closed (code 5, mirror report lane): a wired role needs
    ``profile.roles[role]`` (model; no invented fallback) and the matching
    profile route (``hypotheses``/``literature`` → exactly that role).
    """
    models: dict[str, str] = {}
    prompts: dict[str, PromptRef] = {}
    for role, route_key in _WIRED_STAGE_ROUTES.items():
        if role not in wired_roles:
            continue
        role_config = profile.roles.get(role)
        if role_config is None:
            raise CliRefuse(
                f"profile {profile.name!r} has no {role} role; --wired-roles {role} "
                f"needs profile.roles[{role}].model (no invented fallback)",
                code=5,
            )
        route = profile.routes.get(route_key)
        if route is None:
            raise CliRefuse(
                f"profile {profile.name!r} missing required route {route_key!r} for --wired-roles {role}",
                code=5,
            )
        if route.role != role:
            raise CliRefuse(
                f"profile {profile.name!r} route {route_key!r} role is {route.role!r}, expected {role!r}",
                code=5,
            )
        models[role] = role_config.model
        prompts[role] = _WIRED_ROLE_PROMPTS[role]
    return models, prompts


def _assert_cli_models_match_profile(
    role_models: Mapping[str, str],
    *,
    planner_model: str,
    executor_model: str,
    tz_spec_model: str,
) -> None:
    """CLI --*-model flags confirm profile roles (no silent override)."""
    expected = {
        "planner": planner_model,
        "executor-route": executor_model,
        "tz_spec": tz_spec_model,
    }
    for role, flag_model in expected.items():
        profile_model = role_models[role]
        if flag_model != profile_model:
            raise CliRefuse(
                f"CLI model for {role!r} is {flag_model!r} but profile role model is "
                f"{profile_model!r} (must match; no invented override)",
                code=2,
            )


def _assert_profile_cli_routes(profile: Profile) -> None:
    """Required CLI stages must exist as profile routes with exact role wiring."""
    for route_key, role in _CLI_REQUIRED_ROUTES.items():
        route = profile.routes.get(route_key)
        if route is None:
            raise CliRefuse(
                f"profile {profile.name!r} missing required route {route_key!r}",
                code=2,
            )
        if route.role != role:
            raise CliRefuse(
                f"profile {profile.name!r} route {route_key!r} role is {route.role!r}, expected {role!r}",
                code=2,
            )


def _surface_has_profile_tool(allowed: frozenset[str], tool: str) -> bool:
    """Profile tools are bare catalog names; surface ids are exact ``server:tool``."""
    if tool in allowed:
        return True
    suffix = f":{tool}"
    return any(name.endswith(suffix) and name.count(":") == 1 for name in allowed)


def _surface_tool_id(allowed: frozenset[str], tool: str) -> str | None:
    """Profile bare tool name → the unique exact surface id (``server:tool``)."""
    if tool in allowed:
        return tool
    suffix = f":{tool}"
    matches = [name for name in allowed if name.endswith(suffix) and name.count(":") == 1]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_profile_tools_on_surface(allowed: frozenset[str], tools: Sequence[str], *, role: str) -> tuple[str, ...]:
    """Exact surface ids for a profile role's tools; missing/ambiguous → refuse."""
    resolved: list[str] = []
    missing: list[str] = []
    for tool in tools:
        tool_id = _surface_tool_id(allowed, tool)
        if tool_id is None:
            missing.append(tool)
            continue
        resolved.append(tool_id)
    if missing:
        raise CliRefuse(
            f"profile role {role!r} tools not on ToolRouter surface (no blanket-allow): {missing!r}",
            code=5,
        )
    return tuple(resolved)


def _assert_profile_tools_on_surface(
    profile: Profile, tools: CliToolSurface, *, wired_roles: frozenset[str] = frozenset()
) -> None:
    """Role tools must already be on the cassette/live surface — never blanket-allow.

    Required CLI roles always; wired optional need-stage roles (hypothesizer /
    researcher) when declared in ``--wired-roles`` (fail-closed before any study
    work — a wired LIT without its literature surface refuses here).
    """
    allowed = tools.tool_names
    role_names = [*_CLI_REQUIRED_ROLES, *(r for r in _WIRED_STAGE_ROUTES if r in wired_roles)]
    missing: list[str] = []
    for role_name in role_names:
        role = profile.roles.get(role_name)
        if role is None:
            continue  # wired-role presence is refused by its own check in run_study
        for tool in role.tools:
            if not _surface_has_profile_tool(allowed, tool):
                missing.append(f"{role_name}:{tool}")
    if missing:
        raise CliRefuse(
            f"profile role tools not on ToolRouter surface (no blanket-allow): {missing!r}",
            code=3,
        )


async def _open_tool_surface(
    args: argparse.Namespace,
) -> tuple[CliToolSurface, ToolRouter]:
    coder_backend = getattr(args, "coder_backend", None)
    if args.tools_json is not None:
        tools_payload = json.loads(args.tools_json.read_text(encoding="utf-8"))
        if not isinstance(tools_payload, Mapping):
            raise CliRefuse("--tools-json must be a JSON object", code=2)
        for name, payload in tools_payload.items():
            if type(name) is not str or not name.strip():
                raise CliRefuse("--tools-json keys must be non-empty str", code=2)
            if not isinstance(payload, Mapping):
                raise CliRefuse(f"--tools-json[{name!r}] must be a JSON object", code=2)
        if not tools_payload and coder_backend is None:
            raise CliRefuse(
                "--tools-json is empty and no --coder-backend is set; refuse (empty surface)",
                code=2,
            )
        router = StaticToolRouter(tools=tools_payload)
        # Register cassette declarations through the same closed loader as MCP pins
        # here (single choke point for run AND continue): otherwise a continue-only
        # process freezes the registry without them and every cassette call degrades
        # to the unregistered external_effect default (side_effect deny in headless).
        register_trusted_mcp_tools(router.config)
        if coder_backend is not None:
            router.attach_coder_route(coder_backend)
        return router, router
    bundle_text = args.mcp_tool_bundle.read_text(encoding="utf-8")
    try:
        # JSON text (not a decoded mapping): strict tuple fields accept JSON arrays
        # only through pydantic's JSON parser (same strict model; see mcp_live).
        bundle = load_mcp_tool_router_bundle(bundle_text)
    except ValidationError as exc:
        raise CliRefuse(f"--mcp-tool-bundle invalid: {exc}", code=2) from exc
    allowed_ids = getattr(args, "allowed_tool_ids", None)
    configured_ids = frozenset(f"{route.server_name}:{name}" for route in bundle.router.servers for name in route.tools)
    if allowed_ids is not None and (
        not isinstance(allowed_ids, list) or any(not isinstance(item, str) for item in allowed_ids)
    ):
        raise CliRefuse("allowed_tool_ids must be a JSON array of strings", code=2)
    unknown_ids = frozenset() if allowed_ids is None else frozenset(allowed_ids) - configured_ids
    if unknown_ids:
        raise CliRefuse("allowed_tool_ids must be a subset of trusted bindings", code=2)
    try:
        register_trusted_mcp_tools(bundle.router)
    except ValueError as exc:
        raise CliRefuse(f"trusted tool declarations refused: {exc}", code=2) from exc
    if allowed_ids is not None:
        selected_servers = {
            route.server_name
            for route in bundle.router.servers
            if any(f"{route.server_name}:{tool}" in allowed_ids for tool in route.tools)
        }
        bundle = bundle.model_copy(
            update={
                "router": bundle.router.model_copy(
                    update={
                        "servers": tuple(
                            route for route in bundle.router.servers if route.server_name in selected_servers
                        ),
                    }
                ),
                "client": bundle.client.model_copy(
                    update={
                        "servers": tuple(server for server in bundle.client.servers if server.name in selected_servers),
                    }
                ),
            }
        )
    call_raw = None if args.tool_call_args is None else json.loads(args.tool_call_args.read_text(encoding="utf-8"))
    if call_raw is not None and not isinstance(call_raw, Mapping):
        raise CliRefuse("--tool-call-args must be a JSON object", code=2)
    if call_raw is not None:
        for name, payload in call_raw.items():
            if type(name) is not str or not name.strip():
                raise CliRefuse("--tool-call-args keys must be non-empty str", code=2)
            if not isinstance(payload, Mapping):
                raise CliRefuse(f"--tool-call-args[{name!r}] must be a JSON object", code=2)
    try:
        if bundle.router.servers:
            _client, live = await open_tool_router_for_cli(
                bundle,
                clock=_McpMonoClock(),
                sleep=wall_sleep,
                wait_for=wall_wait_for,
            )
        else:
            live = ToolRouter(bundle.router)
            live.mark_open()
    except (ToolRouterError, McpLiveError, OSError, TimeoutError, ValidationError) as exc:
        raise CliRefuse(f"failed to open MCP ToolRouter: {exc}", code=4) from exc
    if coder_backend is not None:
        # Attach before the surface validates --tool-call-args keys (the reserved
        # "coder" key is only acceptable with an attached route).
        live.attach_coder_route(coder_backend)
    try:
        surface = LiveCliToolSurface(live, call_args=call_raw, allowed_tool_ids=getattr(args, "allowed_tool_ids", None))
        register_trusted_mcp_tools(bundle.router)
    except ValueError as exc:
        await live.aclose()
        raise CliRefuse(f"--tool-call-args refused: {exc}", code=2) from exc
    return surface, live


def _parse_wired_roles(raw: str) -> frozenset[str]:
    if type(raw) is not str:
        raise CliRefuse("--wired-roles must be a str", code=2)
    text = raw.strip()
    if not text:
        return frozenset()
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliRefuse(f"--wired-roles must be a JSON array: {exc}", code=2) from exc
    if not isinstance(loaded, list):
        raise CliRefuse("--wired-roles must be a JSON array", code=2)
    roles: set[str] = set()
    for item in loaded:
        if type(item) is not str or not item.strip():
            raise CliRefuse("--wired-roles entries must be non-empty str", code=2)
        name = item.strip()
        if name not in _OPTIONAL_ROLES:
            raise CliRefuse(
                f"unknown optional role {name!r}; allowed={sorted(_OPTIONAL_ROLES)}",
                code=2,
            )
        roles.add(name)
    return frozenset(roles)


def _parse_bool(raw: str) -> bool:
    key = raw.strip().lower()
    if key == "true":
        return True
    if key == "false":
        return False
    raise argparse.ArgumentTypeError("expected true|false")


def _load_task_inputs(paths: Sequence[Path] | None) -> list[UserInput]:
    """Read ``--task-input`` files into typed ``UserInput`` rows (fail-closed).

    Missing files and duplicate artifact names (file basenames) refuse before
    any study / LLM work; no defaults are guessed (media type comes from the
    fixed suffix map, content is raw bytes, source_uri is the given path).
    """
    if not paths:
        return []
    inputs: list[UserInput] = []
    names: set[str] = set()
    for i, raw in enumerate(paths, start=1):
        if not isinstance(raw, Path):
            raise CliRefuse(f"--task-input #{i} must be a path", code=2)
        if not raw.is_file():
            raise CliRefuse(f"--task-input file not found: {raw}", code=2)
        name = raw.name
        if not name.strip():
            raise CliRefuse(f"--task-input #{i} has an empty file name", code=2)
        if name in names:
            raise CliRefuse(
                f"--task-input duplicate artifact name {name!r} "
                f"(names are file basenames; rename one file to register both)",
                code=2,
            )
        names.add(name)
        try:
            from coscientist.core.execution.input_limits import DEFAULT_MAX_WIRE_BYTES

            if raw.stat().st_size > DEFAULT_MAX_WIRE_BYTES:
                raise CliRefuse(
                    f"--task-input {raw} exceeds {DEFAULT_MAX_WIRE_BYTES} bytes (wire limit; register smaller inputs)",
                    code=2,
                )
            content = raw.read_bytes()
        except OSError as exc:
            raise CliRefuse(f"--task-input {raw} unreadable: {exc}", code=2) from exc
        inputs.append(
            UserInput(
                name=name,
                content=content,
                media_type=_task_input_media_type(raw),
                source_uri=str(raw),
                encoding="raw",
            )
        )
    return inputs


# Route prefixes that map to non-experiment task kinds (core ``_prefix_for_route``);
# code execution is experiment delivery only.
