"""``coscientist run <task>`` — honest study cycle to close (DESIGN §13 / §12.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO

from pydantic import ValidationError

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.derive import CODER_HINT, PlanDeriveError, derive_plan
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    ExecutorRouteIn,
    PlanIntent,
    PlannedToolCall,
    RegisteredInput,
    RubricDraft,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.api.studies import (
    mint_study_user_put_capability,
    release_study_user_put_capability,
)
from coscientist.checks.catalog import load_catalog
from coscientist.checks.sandbox import SandboxLimits, limits_from_check
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.journal import record_executor_route, record_research_spec
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.static_router import StaticToolRouter
from coscientist.cli.tool_surface import LiveCliToolSurface
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.errors import CatalogError
from coscientist.core.events.events import StoredEvent, UnstampedEvent
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.call_ctx import CallCtx
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Budget,
    Catalog,
    HitlPolicy,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.input_limits import UserInput
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.llm.client import LlmClient
from coscientist.llm.config import LlmConfig
from coscientist.profiles.errors import (
    CatalogVersionError,
    ProfileLoadError,
    UnknownProfileError,
)
from coscientist.profiles.load import load_profile
from coscientist.profiles.models import Profile
from coscientist.tools.coder import (
    CoderOutputSpec,
    CoderRequest,
    invoke_coder_for_study,
)
from coscientist.tools.coder.local import CoderBackend, resolve_coder_backend
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore
from coscientist.tools.mcp_live import (
    McpLiveError,
    load_mcp_tool_router_bundle,
    open_tool_router_for_cli,
)
from coscientist.tools.router import ToolRouter, ToolRouterError

_OPTIONAL_ROLES = frozenset({"hypothesizer", "researcher", "report"})
_ROLE_HYP = "hypothesizer"
_ROLE_LIT = "researcher"
_ROLE_REPORT = "report"
# CLI live/cassette path roles/routes from packaged production research.v1 (DESIGN §7.1).
_CLI_REQUIRED_ROLES = ("tz_spec", "planner", "executor-route")
_CLI_REQUIRED_ROUTES: dict[str, str] = {
    "spec": "tz_spec",
    "plan": "planner",
    "execute": "executor-route",
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


def _task_input_media_type(path: Path) -> str:
    media_type = _TASK_INPUT_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is not None:
        return media_type
    return "application/octet-stream"


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


class _McpMonoClock:
    """Monotonic clock for ``open_tool_router_for_cli`` (MCP runtime Protocol)."""

    def monotonic(self) -> float:
        return time.monotonic()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coscientist")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Create a study from <task> and run until closed")
    run_p.add_argument("task", type=Path, help="Path to task text file (request body)")
    run_p.add_argument("--data-root", type=Path, required=True)
    run_p.add_argument("--deadline-s", type=float, required=True)
    run_p.add_argument("--catalog-version", type=int, required=True)
    run_p.add_argument("--catalog", type=Path, required=True, help="Catalog JSON path")
    run_p.add_argument(
        "--profile",
        type=str,
        required=True,
        help="Production profile name (exact catalog; loaded via fail-closed load_profile)",
    )
    run_p.add_argument(
        "--tool-catalog-version",
        type=int,
        required=True,
        help=(
            "Exact pin for packaged profile tool_catalog_version "
            "(same idea as #65 expected_catalog_version; mismatch → refuse)"
        ),
    )
    run_p.add_argument("--owner", type=str, required=True)
    run_p.add_argument("--llm-config", type=Path, required=True, help="LlmConfig JSON")
    run_p.add_argument(
        "--planner-model",
        type=str,
        required=True,
        help="Must equal profile.roles['planner'].model (no invented override)",
    )
    run_p.add_argument(
        "--executor-model",
        type=str,
        required=True,
        help="Must equal profile.roles['executor-route'].model",
    )
    run_p.add_argument(
        "--tz-spec-model",
        type=str,
        required=True,
        help="Must equal profile.roles['tz_spec'].model",
    )
    tools_group = run_p.add_mutually_exclusive_group(required=True)
    tools_group.add_argument(
        "--tools-json",
        type=Path,
        help=(
            'Cassette path: JSON object tool_name → {"result": object, "args": object} for '
            "StaticToolRouter (both keys required; args may be {})"
        ),
    )
    tools_group.add_argument(
        "--mcp-tool-bundle",
        type=Path,
        help=(
            "Live MCP path: JSON ``McpToolRouterBundle`` "
            "(client+router; exact server/tool names; opens real ToolRouter)"
        ),
    )
    run_p.add_argument(
        "--tool-call-args",
        type=Path,
        help=(
            "Required with --mcp-tool-bundle: JSON object exact server:tool → args object "
            "(args may be {}; unknown keys refuse)"
        ),
    )
    run_p.add_argument(
        "--coder-backend",
        type=str,
        choices=("bwrap", "local-python"),
        help=(
            "Explicit coder execution backend for code-execution tasks (tools_hint "
            "'coder'): 'bwrap' (Linux/CI hard sandbox) or 'local-python' (macOS/dev, "
            "no net/fs isolation). This flag is the operator's explicit allow for the "
            "write-effect coder tool; absent → such tasks typed-refuse (no stub science)"
        ),
    )
    run_p.add_argument(
        "--coder-jobs",
        type=Path,
        help=(
            "Required with --coder-backend when the plan has code-execution tasks: "
            "JSON object task_key → {argv: [..], inputs: {name: str}, env: {..}, "
            "outputs: [{name, source: stdout|stderr, media_type}], limits: {timeout_s, "
            "memory_mb, max_output_bytes, pids_max, rlimit_nproc, allow_network, "
            "allow_cgroup_degraded, reap_timeout_s, output_drain_join_s}}. "
            "argv/limits are never invented by the CLI"
        ),
    )
    run_p.add_argument(
        "--wired-roles",
        type=str,
        default="[]",
        help=(
            'JSON array of optional roles to attempt when planner Need is on: '
            '["hypothesizer","researcher","report"] (default [] = fail-closed / cancel)'
        ),
    )
    run_p.add_argument("--study-id", type=str, required=True)
    run_p.add_argument(
        "--task-input",
        type=Path,
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "User task data file registered as a study input through the same "
            "ingest gate as POST /studies (repeatable; artifact name is the file "
            "basename and must be unique)"
        ),
    )
    run_p.add_argument(
        "--require-hypotheses",
        type=_parse_bool,
        required=True,
        help="Study.require_hypotheses (true|false); true needs hypothesizer in --wired-roles",
    )
    run_p.add_argument(
        "--confirmations-needed",
        type=int,
        required=True,
        help="Study.confirmations_needed (PR8 snapshot k; >= 1; no default)",
    )
    run_p.add_argument(
        "--refutations-needed",
        type=int,
        required=True,
        help="Study.refutations_needed (>= 1; no default)",
    )
    run_p.add_argument(
        "--refutation-priority",
        type=_parse_bool,
        required=True,
        help="Study.refutation_priority (true|false; no default)",
    )
    run_p.add_argument(
        "--supported-needed",
        type=int,
        required=True,
        help="Study.supported_needed (>= 2; no default)",
    )
    run_p.add_argument(
        "--max-expansions",
        type=int,
        required=True,
        help="Study.max_expansions agent quota (>= 0; no default)",
    )
    run_p.add_argument("--finalize-reserve-s", type=int, required=True)
    run_p.add_argument("--lease-ttl-s", type=float, required=True)
    run_p.add_argument("--rng-seed", type=int, required=True)
    run_p.add_argument("--max-llm-calls", type=int, required=True)
    run_p.add_argument("--max-tokens", type=int, required=True)
    run_p.add_argument("--max-tool-calls", type=int, required=True)
    run_p.add_argument("--max-ticks", type=int, required=True)
    run_p.add_argument(
        "--hitl-mode",
        type=str,
        required=True,
        choices=("interactive", "auto", "headless"),
        help="HitlPolicy.mode for the study (required; no default)",
    )
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error(f"unknown command {args.command!r}")
    try:
        return asyncio.run(_async_main(args))
    except CliRefuse as exc:
        sys.stderr.write(f"cli refuse: {exc.detail}\n")
        raise SystemExit(exc.code) from None


async def _async_main(args: argparse.Namespace) -> int:
    _validate_tool_cli_flags(args)
    task_inputs = _load_task_inputs(args.task_input)
    wired_roles = _parse_wired_roles(args.wired_roles)
    coder_jobs: dict[str, _CoderJobDecl] = {}
    if args.coder_jobs is not None:
        try:
            coder_jobs = _parse_coder_jobs(json.loads(args.coder_jobs.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise CliRefuse(f"--coder-jobs must be valid JSON: {exc}", code=2) from exc
        if not coder_jobs:
            raise CliRefuse("--coder-jobs must declare at least one task job", code=2)
    loaded_profile = _load_cli_profile(
        args.profile,
        expected_tool_catalog_version=args.tool_catalog_version,
    )
    _assert_profile_cli_routes(loaded_profile)
    if loaded_profile.mcp_servers:
        raise CliRefuse(
            "profile declares mcp_servers; CLI does not invent MCP — "
            "pass exact servers via --mcp-tool-bundle (hard dep until wired)",
            code=5,
        )
    role_models = _role_models_from_profile(loaded_profile)
    _assert_cli_models_match_profile(
        role_models,
        planner_model=args.planner_model,
        executor_model=args.executor_model,
        tz_spec_model=args.tz_spec_model,
    )
    catalog = load_catalog(args.catalog, kind_registry=default_execution_registry())
    llm_config = LlmConfig.model_validate_json(args.llm_config.read_text(encoding="utf-8"))
    llm_clock = WallLlmClock()
    client = LlmClient(
        llm_config,
        HttpxTransport(),
        clock=llm_clock,
        sleep=wall_sleep,
        wait_for=wall_wait_for,
        rng=random.Random(args.rng_seed),
    )
    # Study HitlPolicy.mode comes only from required --hitl-mode (never invented).
    hitl_policy = HitlPolicy(mode=args.hitl_mode)
    study_clock = SystemClock()
    live_to_close: list[ToolRouter] = []
    try:
        tools, tool_router = await _open_tool_surface(args)
        if args.mcp_tool_bundle is not None:
            live_to_close.append(tool_router)
        _assert_profile_tools_on_surface(loaded_profile, tools)
        adapter = LlmAgentAdapter(
            client,
            role_models=role_models,
            tool_router=tool_router,
            prompts=role_prompt_registry(),
            role_prompts={
                "planner": ROLE_PLANNER_REF,
                "executor-route": ROLE_EXECUTOR_REF,
                "tz_spec": ROLE_TZ_SPEC_REF,
            },
            clock=study_clock,
            hitl_policy=hitl_policy,
        )
        return await run_study(
            task_path=args.task,
            data_root=args.data_root,
            deadline_s=args.deadline_s,
            catalog_version=args.catalog_version,
            catalog=catalog,
            loaded_profile=loaded_profile,
            owner=args.owner,
            adapter=adapter,
            out=sys.stdout,
            study_id=args.study_id,
            task_inputs=task_inputs,
            require_hypotheses=args.require_hypotheses,
            confirmations_needed=args.confirmations_needed,
            refutations_needed=args.refutations_needed,
            refutation_priority=args.refutation_priority,
            supported_needed=args.supported_needed,
            max_expansions=args.max_expansions,
            max_attempts=loaded_profile.max_attempts,
            finalize_reserve_s=args.finalize_reserve_s,
            hitl_policy=hitl_policy,
            lease_ttl_s=args.lease_ttl_s,
            max_llm_calls=args.max_llm_calls,
            max_tokens=args.max_tokens,
            max_tool_calls=args.max_tool_calls,
            max_ticks=args.max_ticks,
            tools=tools,
            wired_roles=wired_roles,
            study_clock=study_clock,
            coder_backend=args.coder_backend,
            coder_jobs=coder_jobs,
        )
    finally:
        for live in live_to_close:
            await live.aclose()


def _validate_tool_cli_flags(args: argparse.Namespace) -> None:
    """Fail-closed tool-path flags before catalog/LLM I/O."""
    if args.tools_json is not None and args.tool_call_args is not None:
        raise CliRefuse("--tool-call-args is only valid with --mcp-tool-bundle", code=2)
    if args.mcp_tool_bundle is not None and args.tool_call_args is None:
        raise CliRefuse("--tool-call-args is required with --mcp-tool-bundle", code=2)
    if args.tools_json is None and args.mcp_tool_bundle is None:
        raise CliRefuse("one of --tools-json or --mcp-tool-bundle is required", code=2)
    if args.coder_jobs is not None and args.coder_backend is None:
        raise CliRefuse(
            "--coder-jobs is only valid with --coder-backend (explicit backend required)",
            code=2,
        )


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
    missing = [r for r in _CLI_REQUIRED_ROLES if r not in profile.roles]
    if missing:
        raise CliRefuse(
            f"profile {profile.name!r} missing required roles for CLI path: {missing!r}",
            code=2,
        )
    return {role: profile.roles[role].model for role in _CLI_REQUIRED_ROLES}


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
                f"profile {profile.name!r} route {route_key!r} role is {route.role!r}, "
                f"expected {role!r}",
                code=2,
            )


def _surface_has_profile_tool(allowed: frozenset[str], tool: str) -> bool:
    """Profile tools are bare catalog names; surface ids are exact ``server:tool``."""
    if tool in allowed:
        return True
    suffix = f":{tool}"
    return any(name.endswith(suffix) and name.count(":") == 1 for name in allowed)


def _assert_profile_tools_on_surface(profile: Profile, tools: CliToolSurface) -> None:
    """Role tools must already be on the cassette/live surface — never blanket-allow."""
    allowed = tools.tool_names
    missing: list[str] = []
    for role_name in _CLI_REQUIRED_ROLES:
        for tool in profile.roles[role_name].tools:
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
                "--tools-json is empty and no --coder-backend is set; "
                "refuse (empty surface)",
                code=2,
            )
        router = StaticToolRouter(tools=tools_payload)
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
    call_raw = json.loads(args.tool_call_args.read_text(encoding="utf-8"))
    if not isinstance(call_raw, Mapping):
        raise CliRefuse("--tool-call-args must be a JSON object", code=2)
    for name, payload in call_raw.items():
        if type(name) is not str or not name.strip():
            raise CliRefuse("--tool-call-args keys must be non-empty str", code=2)
        if not isinstance(payload, Mapping):
            raise CliRefuse(f"--tool-call-args[{name!r}] must be a JSON object", code=2)
    try:
        _client, live = await open_tool_router_for_cli(
            bundle,
            clock=_McpMonoClock(),
            sleep=wall_sleep,
            wait_for=wall_wait_for,
        )
    except (ToolRouterError, McpLiveError, OSError, TimeoutError, ValidationError) as exc:
        raise CliRefuse(f"failed to open MCP ToolRouter: {exc}", code=4) from exc
    if coder_backend is not None:
        # Attach before the surface validates --tool-call-args keys (the reserved
        # "coder" key is only acceptable with an attached route).
        live.attach_coder_route(coder_backend)
    try:
        surface = LiveCliToolSurface(live, call_args=call_raw)
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
                    f"--task-input {raw} exceeds {DEFAULT_MAX_WIRE_BYTES} bytes "
                    "(wire limit; register smaller inputs)",
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
_NON_EXPERIMENT_ROUTE_PREFIXES = (
    "literature",
    "lit",
    "hypotheses",
    "hypothesis",
    "hyp",
    "planner",
    "plan",
)


@dataclass(frozen=True)
class _CoderJobDecl:
    """One declared coder job (``--coder-jobs`` JSON row; nothing invented)."""

    argv: tuple[str, ...]
    inputs: dict[str, bytes]
    env: dict[str, str]
    outputs: tuple[CoderOutputSpec, ...]
    limits: SandboxLimits


def _parse_coder_job_decl(task_key: str, raw: object) -> _CoderJobDecl:
    """Fail-closed parse of one ``--coder-jobs`` row (exact keys, typed values)."""
    if not isinstance(raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}] must be a JSON object", code=2)
    for key in ("argv", "outputs", "limits"):
        if key not in raw:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}] missing required key {key!r}", code=2
            )
    argv_raw = raw["argv"]
    if not isinstance(argv_raw, list) or not argv_raw:
        raise CliRefuse(f"--coder-jobs[{task_key!r}].argv must be a non-empty list", code=2)
    argv: list[str] = []
    for token in argv_raw:
        if type(token) is not str or not token:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].argv entries must be non-empty str", code=2
            )
        argv.append(token)
    inputs_raw = raw.get("inputs")
    if inputs_raw is None:
        inputs_raw = {}
    env_raw = raw.get("env")
    if env_raw is None:
        env_raw = {}
    if not isinstance(inputs_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].inputs must be a JSON object", code=2)
    if not isinstance(env_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].env must be a JSON object", code=2)
    inputs: dict[str, bytes] = {}
    for name, content in inputs_raw.items():
        if type(name) is not str or not name:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].inputs keys must be non-empty str", code=2
            )
        if type(content) is not str:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].inputs[{name!r}] must be a str "
                f"(UTF-8 text; binary inputs are not declared this way)",
                code=2,
            )
        inputs[name] = content.encode("utf-8")
    env: dict[str, str] = {}
    for name, value in env_raw.items():
        if type(name) is not str or not name or type(value) is not str:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].env entries must be str → str", code=2
            )
        env[name] = value
    outputs_raw = raw["outputs"]
    if not isinstance(outputs_raw, list) or not outputs_raw:
        raise CliRefuse(
            f"--coder-jobs[{task_key!r}].outputs must be a non-empty list", code=2
        )
    outputs: list[CoderOutputSpec] = []
    for row in outputs_raw:
        if not isinstance(row, Mapping):
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs entries must be JSON objects", code=2
            )
        source = row.get("source")
        if source not in ("stdout", "stderr"):
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs[].source must be 'stdout'|'stderr'",
                code=2,
            )
        name = row.get("name")
        media_type = row.get("media_type")
        if type(name) is not str or type(media_type) is not str:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs[] 'name'/'media_type' must be str",
                code=2,
            )
        try:
            outputs.append(
                CoderOutputSpec(name=name, source=source, media_type=media_type)
            )
        except ToolError as exc:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs[] refused: {exc.detail}", code=2
            ) from exc
    limits_raw = raw["limits"]
    if not isinstance(limits_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].limits must be a JSON object", code=2)
    try:
        limits = limits_from_check(**dict(limits_raw))
    except TypeError as exc:
        raise CliRefuse(
            f"--coder-jobs[{task_key!r}].limits keys refused "
            f"(exactly the 9 budget keys; no extras): {exc}",
            code=2,
        ) from exc
    except CatalogError as exc:
        raise CliRefuse(f"--coder-jobs[{task_key!r}].limits refused: {exc}", code=2) from exc
    return _CoderJobDecl(
        argv=tuple(argv),
        inputs=inputs,
        env=env,
        outputs=tuple(outputs),
        limits=limits,
    )


def _parse_coder_jobs(raw: object) -> dict[str, _CoderJobDecl]:
    """Parse the ``--coder-jobs`` JSON object (task_key → declared job)."""
    if not isinstance(raw, Mapping):
        raise CliRefuse("--coder-jobs must be a JSON object (task_key → job)", code=2)
    decls: dict[str, _CoderJobDecl] = {}
    for task_key, row in raw.items():
        if type(task_key) is not str or not task_key.strip():
            raise CliRefuse("--coder-jobs keys must be non-empty str", code=2)
        decls[task_key] = _parse_coder_job_decl(task_key, row)
    return decls


class _WriterJobJournal:
    """Sync ``JobJournal`` → async ``StudyWriter.append`` (drained by the dispatcher).

    ``InMemoryJobStore`` emits sync; the study journal is an asyncio queue. Each
    event becomes a writer task; ``drain`` must complete before the dispatch
    path returns so Job* events are durably journaled in order with the effect
    events ``invoke_tool`` journals through the same writer.
    """

    def __init__(self, writer: StudyWriter) -> None:
        self._writer = writer
        self._pending: set[asyncio.Task[object]] = set()

    def record(self, event: UnstampedEvent) -> None:
        task = asyncio.get_running_loop().create_task(self._writer.append(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self, *, strict: bool) -> None:
        while self._pending:
            pending = tuple(self._pending)
            if strict:
                await asyncio.gather(*pending)
            else:
                await asyncio.gather(*pending, return_exceptions=True)


def _coder_hitl_policy(policy: HitlPolicy) -> HitlPolicy:
    """Coder dispatch policy: the operator's explicit ``--coder-backend`` allow.

    The write-effect ``side_effect`` gate (DESIGN_M1 §8) needs an explicit allow;
    passing ``--coder-backend`` IS that operator decision for exactly the coder
    tool — scoped to this ctx, never written back into the study policy.
    """
    return HitlPolicy(
        mode=policy.mode,
        defaults={**policy.defaults, "side_effect": "allow"},
    )


async def _dispatch_coder_task(
    rt: StudyRuntime,
    *,
    draft: TaskSpecDraft,
    study_id: str,
    backend: CoderBackend,
    jobs_decl: Mapping[str, _CoderJobDecl],
    coder_task_keys: frozenset[str],
    writer_journal: _WriterJobJournal,
    clock: Clock,
    hitl_policy: HitlPolicy,
    deadline: datetime,
    usages: list[dict[str, object]],
) -> None:
    """Dispatch one code-execution task via ``invoke_coder_for_study`` (honest path).

    Typed refuses (fail-closed, no stub science): mixed hints, non-experiment
    route, missing declared job, expected outputs not covered by the declared
    job outputs, non-succeeded job outcome. Job artifacts are registered by the
    coder backend itself (sandbox producer) — model-invented values never enter.
    """
    if len(draft.tools_hint) != 1:
        raise CliRefuse(
            f"task {draft.task_key!r} tools_hint {list(draft.tools_hint)!r}: "
            f"code-execution delivery ({CODER_HINT!r}) must be the only hint",
            code=3,
        )
    route = draft.route.strip().lower()
    if route in _NON_EXPERIMENT_ROUTE_PREFIXES:
        raise CliRefuse(
            f"task {draft.task_key!r} route {draft.route!r} is a non-experiment "
            f"kind; code execution is experiment delivery only",
            code=3,
        )
    decl = jobs_decl.get(draft.task_key)
    if decl is None and len(jobs_decl) == 1 and len(coder_task_keys) == 1:
        # Planner assigns task keys; the operator cannot know them in advance.
        # Exactly one coder task and one declared job is unambiguous — bind.
        decl = next(iter(jobs_decl.values()))
    if decl is None:
        raise CliRefuse(
            f"task {draft.task_key!r} needs a declared coder job "
            f"(--coder-jobs[{draft.task_key!r}]); known={sorted(jobs_decl)!r}",
            code=3,
        )
    missing_outputs = [
        o.name for o in draft.expected_outputs if o.name not in {s.name for s in decl.outputs}
    ]
    if missing_outputs:
        raise CliRefuse(
            f"task {draft.task_key!r} expected outputs {missing_outputs!r} not declared "
            f"in --coder-jobs[{draft.task_key!r}].outputs "
            f"(declared={[s.name for s in decl.outputs]!r})",
            code=3,
        )
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    aid = await _require_attempt(rt, draft.task_key)

    def _attempt_is_closed() -> bool:
        attempt = rt.attempts.get(aid)
        if isinstance(attempt, Mapping):
            closed = attempt.get("closed")
            if isinstance(closed, bool):
                return closed
        return False

    request = CoderRequest(
        attempt_id=aid,
        call_id=f"{aid}:coder",
        argv=decl.argv,
        inputs=decl.inputs,
        env=decl.env,
        limits=decl.limits,
        deadline=deadline,
        outputs=decl.outputs,
        attempt_is_closed=_attempt_is_closed,
    )
    jobs = InMemoryJobStore(
        study_id=study_id,
        clock=clock,
        journal=writer_journal,
        on_clock_change=None,
    )
    ctx = CallCtx(
        study_id=study_id,
        task_key=draft.task_key,
        attempt_id=aid,
        hitl_policy=_coder_hitl_policy(hitl_policy),
    )
    try:
        outcome, tool_out = await invoke_coder_for_study(
            request,
            backend=backend,
            jobs=jobs,
            writer=rt.writer,
            artifacts=rt.executor.artifacts,
            ctx=ctx,
        )
    except ToolError as exc:
        await writer_journal.drain(strict=False)
        raise CliRefuse(
            f"coder invoke for task {draft.task_key!r} refused "
            f"(backend={backend!r}): {exc.detail}",
            code=4,
        ) from exc
    except BaseException:  # audit-ok[broad_except]: best-effort drain before re-raise, exception propagates
        await writer_journal.drain(strict=False)
        raise
    await writer_journal.drain(strict=True)
    if outcome.status != "succeeded":
        raise CliRefuse(
            f"coder job for task {draft.task_key!r} {outcome.status} "
            f"(retryable={outcome.retryable}, detail={outcome.detail!r}) — refuse",
            code=4,
        )
    outputs: dict[str, ArtifactHandle] = {}
    for art in tool_out.artifacts:
        outputs[art.name] = ArtifactHandle(artifact_id=art.artifact_id, name=art.name)
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=f"coder job {outcome.job_id}: {outcome.detail}",
            outputs=outputs,
            values=outcome.model_dump(mode="json"),
            claims=[],
        ),
    )
    usages.append(
        {
            "role": "coder",
            "task_key": draft.task_key,
            "job_id": outcome.job_id,
            "status": outcome.status,
            "artifact_ids": list(outcome.artifact_ids),
        }
    )


async def run_study(
    *,
    task_path: Path,
    data_root: Path,
    deadline_s: float,
    catalog_version: int,
    catalog: Catalog,
    loaded_profile: Profile,
    owner: str,
    adapter: LlmAgentAdapter,
    out: TextIO,
    study_id: str,
    task_inputs: Sequence[UserInput] = (),
    require_hypotheses: bool,
    confirmations_needed: int,
    refutations_needed: int,
    refutation_priority: bool,
    supported_needed: int,
    max_expansions: int,
    max_attempts: int,
    finalize_reserve_s: int,
    lease_ttl_s: float,
    max_llm_calls: int,
    max_tokens: int,
    max_tool_calls: int,
    max_ticks: int,
    tools: CliToolSurface,
    hitl_policy: HitlPolicy,
    wired_roles: frozenset[str] = frozenset(),
    study_clock: Clock | None = None,
    coder_backend: CoderBackend | None = None,
    coder_jobs: Mapping[str, _CoderJobDecl] | None = None,
) -> int:
    """Create study, run stages → plan → experiments via ``Executor.record``, tick to close."""
    if not isinstance(loaded_profile, Profile):
        raise CliRefuse(
            f"loaded_profile must be Profile, got {type(loaded_profile).__name__}",
            code=2,
        )
    _assert_profile_cli_routes(loaded_profile)
    _assert_profile_tools_on_surface(loaded_profile, tools)
    profile_name = loaded_profile.name
    if require_hypotheses and _ROLE_HYP not in wired_roles:
        raise CliRefuse(
            "require_hypotheses=true needs hypothesizer in --wired-roles "
            "(and LlmAgentAdapter schema; hard dep until adapter wires the role)",
            code=5,
        )
    request_text = task_path.read_text(encoding="utf-8").strip()
    if not request_text:
        raise CliRefuse("task file must be non-empty", code=2)
    if not (isinstance(deadline_s, (int, float)) and deadline_s > 0):
        raise CliRefuse("deadline_s must be > 0", code=2)
    if type(catalog_version) is not int:
        raise CliRefuse("catalog_version must be int", code=2)
    if type(study_id) is not str or not study_id.strip():
        raise CliRefuse("study_id must be a non-empty str", code=2)
    if type(confirmations_needed) is not int or confirmations_needed < 1:
        raise CliRefuse("confirmations_needed must be int >= 1", code=2)
    if type(refutations_needed) is not int or refutations_needed < 1:
        raise CliRefuse("refutations_needed must be int >= 1", code=2)
    if type(refutation_priority) is not bool:
        raise CliRefuse("refutation_priority must be bool", code=2)
    if type(supported_needed) is not int or supported_needed < 2:
        raise CliRefuse("supported_needed must be int >= 2", code=2)
    if type(max_expansions) is not int or max_expansions < 0:
        raise CliRefuse("max_expansions must be int >= 0", code=2)
    if type(max_attempts) is not int or max_attempts < 1:
        raise CliRefuse("max_attempts must be int >= 1 (from profile)", code=2)
    if type(finalize_reserve_s) is not int or finalize_reserve_s < 0:
        raise CliRefuse("finalize_reserve_s must be int >= 0", code=2)
    if not (isinstance(lease_ttl_s, (int, float)) and lease_ttl_s > 0):
        raise CliRefuse("lease_ttl_s must be > 0", code=2)
    if type(max_ticks) is not int or max_ticks < 1:
        raise CliRefuse("max_ticks must be int >= 1", code=2)
    if type(max_llm_calls) is not int or max_llm_calls < 1:
        raise CliRefuse("max_llm_calls must be int >= 1", code=2)
    if type(max_tokens) is not int or max_tokens < 1:
        raise CliRefuse("max_tokens must be int >= 1", code=2)
    if type(max_tool_calls) is not int or max_tool_calls < 0:
        raise CliRefuse("max_tool_calls must be int >= 0", code=2)
    unknown_wired = sorted(r for r in wired_roles if r not in _OPTIONAL_ROLES)
    if unknown_wired:
        raise CliRefuse(f"wired_roles unknown: {unknown_wired!r}", code=2)
    allowed_tool_names = tools.tool_names
    allow_tools = list(tools.named_tools())
    # Explicit backend only (PR #207 semantics); the surface must carry the
    # coder binding exactly when a backend is given (no silent platform guess).
    if coder_backend is not None:
        try:
            resolve_coder_backend(coder_backend)
        except ToolError as exc:
            raise CliRefuse(f"coder backend refused: {exc.detail}", code=2) from exc
        if CODER_HINT not in allowed_tool_names:
            raise CliRefuse(
                f"coder_backend={coder_backend!r} but the tool surface has no coder "
                f"binding (attach --coder-backend when opening the surface)",
                code=2,
            )
    if coder_jobs and coder_backend is None:
        raise CliRefuse("coder_jobs requires coder_backend (explicit backend)", code=2)

    if study_clock is not None:
        wall = study_clock
    else:
        wall = SystemClock()
    deadline = wall.now() + timedelta(seconds=float(deadline_s))
    data_root.mkdir(parents=True, exist_ok=True)
    opened = await open_runtime(
        data_root,
        study_id,
        catalog=catalog,
        clock=wall,
        owner=owner,
        runtime_cls=StudyRuntime,
    )
    if not isinstance(opened, StudyRuntime):
        raise CliRefuse(
            f"open_runtime returned {type(opened).__name__}, expected StudyRuntime",
            code=4,
        )
    rt = opened
    usages: list[dict[str, object]] = []
    writer_job_journal = _WriterJobJournal(rt.writer)
    try:
        if not isinstance(hitl_policy, HitlPolicy):
            raise CliRefuse(
                f"hitl_policy must be HitlPolicy, got {type(hitl_policy).__name__}",
                code=2,
            )
        # Same ingest gate as POST /studies: inputs go through
        # register_user_input_artifacts inside create_study (CAS artifacts +
        # ArtifactRegistered, producer=user) under a capability minted by the
        # intake module — the CLI never touches the gate directly.
        user_cap = mint_study_user_put_capability() if task_inputs else None
        try:
            await rt.create_study(
                profile=profile_name,
                request=request_text,
                catalog_version=catalog_version,
                deadline=deadline,
                sid=study_id,
                require_hypotheses=require_hypotheses,
                confirmations_needed=confirmations_needed,
                refutations_needed=refutations_needed,
                refutation_priority=refutation_priority,
                supported_needed=supported_needed,
                max_expansions=max_expansions,
                max_attempts=max_attempts,
                finalize_reserve_s=finalize_reserve_s,
                hitl_policy=hitl_policy,
                # Explicit user blobs (no invent); empty when no --task-input.
                inputs=list(task_inputs),
                user_put_capability=user_cap,
            )
        finally:
            release_study_user_put_capability(user_cap)
        refusal = await _study_refused_detail(rt)
        if refusal is not None:
            # Fail-closed: a refused create (input limits, decode, duration)
            # must not fall through to a confusing start_attempt refusal.
            raise CliRefuse(f"create_study refused: {refusal}", code=3)
        registered_inputs = await _registered_user_inputs(rt)
        if len(registered_inputs) != len(task_inputs):
            raise CliRefuse(
                f"registered user inputs ({len(registered_inputs)}) do not match "
                f"--task-input files ({len(task_inputs)}); refusing (no silent drop)",
                code=4,
            )
        await rt.ensure_lease(ttl_s=float(lease_ttl_s))

        # Cumulative session budget from CLI flags (preserved); profile routes validated above.
        role_budget = Budget(
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            max_cost_rub=None,
            max_tool_calls=max_tool_calls,
            deadline=deadline,
        )
        ctx = StudyContext(
            study_id=study_id,
            request=request_text,
            profile=profile_name,
            criterion_template_ids=tuple(sorted(t.id for t in catalog.templates)),
            tool_ids=tuple(sorted(allowed_tool_names)),
            # Registered study inputs: the planner plans against names + refs
            # (never prompt-path guessing).
            inputs=registered_inputs,
        )

        # SPEC — tz_spec agent → Executor.record.
        spec_aid = await _require_attempt(rt, "SPEC")
        spec_run = await adapter.run("tz_spec", ctx, [], role_budget)
        usages.append(_usage_row("tz_spec", spec_run))
        if spec_run.error is not None:
            raise CliRefuse(f"tz_spec error: {spec_run.error}", code=4)
        if not isinstance(spec_run.output, ResearchSpec):
            raise CliRefuse("tz_spec must return ResearchSpec", code=4)
        await record_research_spec(rt, spec_aid, spec_run.output)

        # Planner before HYP/LIT close so Need is known (no fabricated SUCCESS).
        plan_run = await adapter.run("planner", ctx, [], role_budget)
        usages.append(_usage_row("planner", plan_run))
        if plan_run.error is not None:
            raise CliRefuse(f"planner error: {plan_run.error}", code=4)
        intent_out = plan_run.output
        if not isinstance(intent_out, PlanIntent):
            raise CliRefuse("planner must return PlanIntent", code=4)
        try:
            plan_out = derive_plan(
                intent_out,
                catalog_templates=catalog.templates,
                tool_ids=sorted(allowed_tool_names),
            )
        except PlanDeriveError as exc:
            raise CliRefuse(f"plan derivation refused: {exc.detail}", code=3) from exc

        await _optional_need_stage(
            rt,
            adapter=adapter,
            ctx=ctx,
            role_budget=role_budget,
            usages=usages,
            task_key="HYP",
            role=_ROLE_HYP,
            needed=plan_out.hypotheses.needed,
            wired_roles=wired_roles,
            need_off_detail="need_off; hypothesizer not wired",
            need_label="hypotheses",
        )
        await _optional_need_stage(
            rt,
            adapter=adapter,
            ctx=ctx,
            role_budget=role_budget,
            usages=usages,
            task_key="LIT",
            role=_ROLE_LIT,
            needed=plan_out.literature.needed,
            wired_roles=wired_roles,
            need_off_detail="need_off; researcher not wired",
            need_label="literature",
        )

        plan_id = await rt.accept_plan(
            tasks=[_to_task_spec(t) for t in plan_out.tasks],
            questions=[QuestionSpec(id=q.id, text=q.text, final_type=q.final_type) for q in plan_out.questions],
            # Main requires these keyword args (no invent / no Optional defaults).
            subquestions=[],
            spec_output=None,
            expand_after=[],
            loops=[],
            hypotheses=plan_out.hypotheses,
            literature=plan_out.literature,
        )
        if not plan_id:
            detail = await _plan_refused_detail(rt)
            if detail:
                msg = f"accept_plan refused plan: {detail}"
            else:
                msg = "accept_plan refused plan: empty plan_id (no PlanRefused detail)"
            raise CliRefuse(msg, code=3)

        coder_task_keys = {
            draft.task_key for draft in plan_out.tasks if CODER_HINT in draft.tools_hint
        }
        for draft in plan_out.tasks:
            dispatchable = {t.spec.task_key for t in rt.iter_dispatchable_tasks()}
            if draft.task_key not in dispatchable:
                raise CliRefuse(
                    f"task {draft.task_key!r} not dispatchable (deps/state); refuse",
                    code=3,
                )
            if CODER_HINT in draft.tools_hint:
                # Code-execution delivery: dispatch the declared coder job through
                # the coder backend (JobStore lifecycle + job artifacts). Typed
                # refuse without explicit --coder-backend (no stub science).
                if coder_backend is None:
                    raise CliRefuse(
                        f"task {draft.task_key!r} needs code execution "
                        f"(tools_hint {CODER_HINT!r}) but --coder-backend is not set; "
                        f"refuse (no stub science; explicit bwrap|local-python required)",
                        code=5,
                    )
                if not coder_jobs:
                    raise CliRefuse(
                        f"task {draft.task_key!r} needs code execution but no coder jobs "
                        f"are declared (--coder-jobs); refuse (argv is never invented)",
                        code=3,
                    )
                await _dispatch_coder_task(
                    rt,
                    draft=draft,
                    study_id=study_id,
                    backend=coder_backend,
                    jobs_decl=coder_jobs,
                    coder_task_keys=frozenset(coder_task_keys),
                    writer_journal=writer_job_journal,
                    clock=wall,
                    hitl_policy=hitl_policy,
                    deadline=deadline,
                    usages=usages,
                )
                continue
            unknown = [h for h in draft.tools_hint if h not in allowed_tool_names]
            if unknown:
                raise CliRefuse(
                    f"task {draft.task_key!r} tools_hint {unknown!r} not in router allow-list",
                    code=3,
                )
            # Resolve call args before start_attempt — KeyError must not leave AttemptStarted.
            try:
                planned = tools.planned_calls(draft.tools_hint)
            except KeyError as exc:
                # planned_calls always raises KeyError(hint) with one arg.
                missing = exc.args[0]
                raise CliRefuse(
                    f"task {draft.task_key!r} tools_hint missing declared call args "
                    f"for {missing!r} (fail-closed before start_attempt)",
                    code=3,
                ) from exc
            aid = await _require_attempt(rt, draft.task_key)
            exec_run = await adapter.run(
                "executor-route",
                ExecutorRouteIn(
                    task=draft,
                    study_id=study_id,
                    attempt_id=aid,
                    tool_args=planned,
                    # Study-level inputs wired to every derived task — the same
                    # uniform binding core applies to attempts
                    # (study_user_input_bindings); derivation-side, not LLM-chosen.
                    inputs=registered_inputs,
                ),
                list(allow_tools),
                role_budget,
            )
            usages.append(_usage_row("executor-route", exec_run, task_key=draft.task_key))
            await record_executor_route(rt, aid, exec_run)

        # REPORT: cancel when unwired; when wired, run honestly (no fake SUCCESS).
        await _optional_report_stage(
            rt,
            adapter=adapter,
            ctx=ctx,
            role_budget=role_budget,
            usages=usages,
            wired_roles=wired_roles,
        )

        ticks = 0
        while rt.view().phase != "closed":
            if ticks >= max_ticks:
                raise CliRefuse(
                    f"max_ticks={max_ticks} exhausted before phase=closed "
                    f"(phase={rt.view().phase!r}, verdict={rt.view().verdict!r})",
                    code=6,
                )
            await rt.ensure_lease(ttl_s=float(lease_ttl_s))
            await rt.tick()
            ticks += 1

        view = rt.view()
        out.write(
            json.dumps(
                {
                    "study_id": study_id,
                    "phase": view.phase,
                    "verdict": view.verdict,
                    "usage": usages,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        if view.verdict != "fulfilled":
            raise CliRefuse(
                f"study closed with verdict={view.verdict!r} (want fulfilled)",
                code=7,
            )
        return 0
    except CliRefuse:
        raise
    except AgentError as exc:
        raise CliRefuse(f"agent error: {exc.detail}", code=4) from exc
    finally:
        await close_runtime(rt)


async def _optional_need_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    role: str,
    needed: bool,
    wired_roles: frozenset[str],
    need_off_detail: str,
    need_label: str,
) -> None:
    """Need-off → official SKIPPED; Need-on + unwired → refuse; wired → refuse until journal."""
    # adapter/ctx/budget/usages reserved for start→run→record once role journal exists.
    _ = (adapter, ctx, role_budget, usages)
    if not needed:
        await rt.cancel_task(task_key, reason="cancelled", detail=need_off_detail)
        return
    if role not in wired_roles:
        raise CliRefuse(
            f"planner set {need_label}.needed=true but {role} role is not wired",
            code=5,
        )
    if not LlmAgentAdapter.supports_role(role):
        raise CliRefuse(
            f"{role} declared in --wired-roles but LlmAgentAdapter has no output schema; "
            f"hard dep: adapter role + catalog/profile (#65) / sandbox (#150) — "
            f"do not stub science",
            code=5,
        )
    # Adapter can run, but CLI has no role journal record yet — refuse before
    # start_attempt rather than leave AttemptStarted without record/cancel.
    raise CliRefuse(
        f"{role} declared in --wired-roles and adapter supports the role, but "
        f"CLI journal record for {need_label} is not wired; "
        f"refuse before start_attempt (no dangling AttemptStarted) — "
        f"hard dep: role journal + catalog/profile (#65) / sandbox (#150)",
        code=5,
    )


async def _optional_report_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    wired_roles: frozenset[str],
) -> None:
    # adapter/ctx/budget/usages reserved for start→run→record once report journal exists.
    _ = (adapter, ctx, role_budget, usages)
    if _ROLE_REPORT not in wired_roles:
        await rt.cancel_task("REPORT", reason="cancelled", detail="report agent not wired")
        return
    if not LlmAgentAdapter.supports_role(_ROLE_REPORT):
        raise CliRefuse(
            "report declared in --wired-roles but LlmAgentAdapter has no output schema; "
            "hard dep: report agent schema + catalog/profile — do not stub science",
            code=5,
        )
    raise CliRefuse(
        "report declared in --wired-roles and adapter supports the role, but "
        "CLI journal record for report is not wired; "
        "refuse before start_attempt (no dangling AttemptStarted) — "
        "hard dep: report journal + catalog/profile",
        code=5,
    )


async def _require_attempt(rt: StudyRuntime, task_key: str) -> str:
    aid = await rt.start_attempt(task_key)
    if aid is None:
        raise CliRefuse(f"start_attempt({task_key!r}) returned None (refused)", code=3)
    return aid


async def _study_refused_detail(rt: StudyRuntime) -> str | None:
    """Last ``StudyRefused.payload`` (kind + reason) from the journal, if any."""
    events = await rt.store.read_all()
    for ev in reversed(events):
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "StudyRefused":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        reason = payload.get("reason")
        if type(kind) is str and type(reason) is str and reason.strip():
            return f"{kind}: {reason.strip()}"
        return "unknown reason"
    return None


async def _registered_user_inputs(rt: StudyRuntime) -> tuple[RegisteredInput, ...]:
    """Journal-sourced registered user inputs (``ArtifactRegistered`` producer=user).

    Typed readback of exactly what the ingest gate journaled — names + CAS
    refs for the planner/executor contexts; never re-derived from CLI paths.
    """
    events = await rt.store.read_all()
    out: list[RegisteredInput] = []
    for ev in events:
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "ArtifactRegistered":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("producer") != "user":
            continue
        name = payload.get("name")
        artifact_id = payload.get("artifact_id")
        uri = payload.get("uri")
        media_type = payload.get("media_type")
        if not (
            type(name) is str
            and name.strip()
            and type(artifact_id) is str
            and artifact_id.strip()
            and type(uri) is str
            and uri.strip()
            and type(media_type) is str
            and media_type.strip()
        ):
            raise CliRefuse(
                f"ArtifactRegistered(user) payload missing typed fields: {payload!r}",
                code=4,
            )
        out.append(
            RegisteredInput(
                name=name,
                artifact_id=artifact_id,
                uri=uri,
                media_type=media_type,
            )
        )
    return tuple(out)


async def _plan_refused_detail(rt: StudyRuntime) -> str:
    """Last ``PlanRefused.payload.detail`` from the journal, if any."""
    events = await rt.store.read_all()
    for ev in reversed(events):
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "PlanRefused":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        detail = payload.get("detail")
        if type(detail) is str and detail.strip():
            return detail.strip()
    return ""


def _usage_row(role: str, run: AgentRun, *, task_key: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "role": role,
        "prompt_tokens": run.usage.prompt_tokens,
        "completion_tokens": run.usage.completion_tokens,
        "latency_ms": run.usage.latency_ms,
        "tool_calls": [c.model_dump() for c in run.tool_calls],
    }
    if task_key is not None:
        row["task_key"] = task_key
    return row


def _rubric_from_draft(draft: RubricDraft) -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=it.id,
                    text=it.text,
                    required=it.required,
                    check=it.check,
                    code_kind=it.code_kind,
                )
                for it in draft.items
            ],
            aggregate=draft.aggregate,
            k=draft.k,
        )
    )


def _to_task_spec(draft: TaskSpecDraft) -> TaskSpec:
    """Map planner draft → core ``TaskSpec``. Rubrics come only from the draft."""
    outputs: list[OutputSpec] = []
    for o in draft.expected_outputs:
        if o.rubric is None:
            item_rubric = None
        else:
            item_rubric = _rubric_from_draft(o.rubric)
        outputs.append(
            OutputSpec(
                name=o.name,
                media_type=o.media_type,
                schema_ref=o.schema_ref,
                required=o.required,
                rubric=item_rubric,
            )
        )
    return TaskSpec(
        task_key=draft.task_key,
        title=draft.title,
        goal=draft.goal,
        route=draft.route,
        depends_on=list(draft.depends_on),
        addresses=list(draft.addresses),
        tools_hint=list(draft.tools_hint),
        expected_outputs=outputs,
        criteria=[RequirementRef(template_id=c) for c in draft.criteria],
    )


__all__ = ["main", "run_study"]
