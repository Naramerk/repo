"""``coscientist run <task>`` — honest study cycle to close (DESIGN §13 / §12.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO, TypeVar

from pydantic import ValidationError

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.critic import CriticVerdict
from coscientist.agents.derive import CODER_HINT, PlanDeriveError, derive_plan
from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.prompts.roles import (
    ROLE_CRITIC_REF,
    ROLE_EXECUTOR_REF,
    ROLE_HYPOTHESIZER_REF,
    ROLE_PLANNER_REF,
    ROLE_RESEARCHER_REF,
    ROLE_REVIEWER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import (
    HYPOTHESES_STAGE_REQUIRED_PIN,
    AgentError,
    AgentRun,
    AutomaticExecutorIn,
    CriterionOutputPin,
    CriticIn,
    ExecutorRouteIn,
    HypothesisOut,
    LiteratureOut,
    PlanIntent,
    PlannedToolCall,
    PlanOut,
    RegisteredInput,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.api.studies import (
    mint_study_user_put_capability,
    release_study_user_put_capability,
)
from coscientist.checks.catalog import load_catalog
from coscientist.cli.coder_jobs import (
    _bind_coder_jobs,
    _CoderJobDecl,
    _dispatch_coder_task,
    _parse_coder_jobs,
    _WriterJobJournal,
)
from coscientist.cli.coder_remote import (
    _dispatch_remote_coder_task,
    _remote_coder_settings_from_args,
    _validate_generated_coverage,  # noqa: F401 — re-export for tests
    compose_remote_coder_runtime,
)
from coscientist.cli.control import StudyControl
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.journal import (
    record_executor_route,
    record_hypotheses,
    record_literature,
    record_research_spec,
)
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.report_lane import (
    SCIENCE_ARTIFACT_NAME,
    human_gate_feedback,
    put_science_report,
    render_report_docs,
    run_report_lane,
)
from coscientist.cli.review_lane import run_review_lane
from coscientist.cli.static_router import StaticToolRouter
from coscientist.cli.study_helpers import (
    _observed_quality,
    _plan_refused_detail,
    _registered_user_inputs,
    _require_attempt,
    _study_refused_detail,
    _to_task_spec,
    _usage_row,
)
from coscientist.cli.tool_execution import RuntimeMcpExecutor
from coscientist.cli.tool_surface import LiveCliToolSurface, register_trusted_mcp_tools
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.claims.reasons import CloseReason
from coscientist.core.events.attempt_fields import attempt_row_is_open
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.execution.domain import (
    Budget,
    Catalog,
    CodeCheck,
    HitlPolicy,
    QuestionSpec,
    RequirementTemplate,
    Task,
    TaskKind,
    TaskOutcome,
    TaskState,
)
from coscientist.core.execution.input_limits import UserInput
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.llm.client import LlmClient
from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import LlmError
from coscientist.profiles.errors import (
    CatalogVersionError,
    ProfileLoadError,
    UnknownProfileError,
)
from coscientist.profiles.load import load_profile
from coscientist.profiles.models import Profile
from coscientist.reporting.science import build_science_report_markdown
from coscientist.tools.coder.local import CoderBackend, resolve_coder_backend
from coscientist.tools.coder.remote_runtime import (
    RemoteCoderRuntime,
    RemoteCoderSettings,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.mcp_live import (
    McpLiveError,
    load_mcp_tool_router_bundle,
    open_tool_router_for_cli,
)
from coscientist.tools.router import ToolRouter, ToolRouterError


def describe_cli_capabilities(
    *, profile_name: str, tool_catalog_version: int, bundle_path: Path | None,
) -> tuple[dict[str, str], dict[str, tuple[str, tuple[str, ...]]]]:
    """Read trusted CLI resources; expose only models, names and binding hashes.

    API composition uses its existing CLI boundary, without importing transports
    or tool implementations into the web layer. No session or tool is opened.
    """
    try:
        profile = load_profile(profile_name, expected_tool_catalog_version=tool_catalog_version)
        servers: dict[str, tuple[str, tuple[str, ...]]] = {}
        if bundle_path is not None:
            bundle = load_mcp_tool_router_bundle(bundle_path.read_text(encoding="utf-8"))
            connections = {server.name: server for server in bundle.client.servers}
            for server in bundle.router.servers:
                digest = sha256_hex(canonical_json({
                    "connection": connections[server.server_name].model_dump(mode="json"),
                    "binding": server.model_dump(mode="json"),
                }))
                servers[server.server_name] = (digest, tuple(sorted(server.tools)))
        return {name: role.model for name, role in profile.roles.items()}, servers
    except (ProfileLoadError, McpLiveError) as exc:
        raise ValueError("configured_capabilities_unavailable") from exc


_OPTIONAL_ROLES = frozenset({"hypothesizer", "researcher", "report", "critic", "reviewer"})
_ROLE_HYP = "hypothesizer"
_ROLE_LIT = "researcher"
_ROLE_REPORT = "report"
_ROLE_CRITIC = "critic"
_ROLE_REVIEWER = "reviewer"
# Stage placeholders driven by their own lanes (SPEC/PLAN rounds, optional
# HYP/LIT stages, the REPORT DAG placeholder closed pre-loop). They never
# legitimately reach the run dispatch pass pending; a runtime pass sighting
# one refuses typed instead of silently re-running a stage role.
_STAGE_TASK_KEYS: frozenset[str] = frozenset({"SPEC", "PLAN", "HYP", "LIT", "REPORT"})
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
# Exit-code contract (also asserted in tests/cli/test_run_study.py; keep in
# sync with CHANGELOG). Exit 0 only on verdict=fulfilled — never weakened.
_RC_EPILOG = """\
exit codes:
  0  study closed with verdict=fulfilled
  2  CLI usage/config refusal (bad flags, profile/catalog mismatch)
  3  typed refusal: create_study / plan derivation / accept_plan
     (plan refusals get ONE bounded replan round before refusing)
  4  agent failure outside the bounded retry (budget exhausted, adapter
     contract drift); transient executor failures retry via the core
  5  missing wiring / hard dependency (wired role schema, coder backend)
  6  max_ticks exhausted before phase=closed (recovery-loop cap)
  7  study closed with a non-fulfilled verdict (incl. retry exhaustion)
  8  post-finalize lane refusal (report / review lane hard failure)
"""


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
    run_p = sub.add_parser(
        "run",
        help="Create a study from <task> and run until closed",
        epilog=_RC_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
            "Optional fixed-recipe compat input with --mcp-tool-bundle: JSON object "
            "exact server:tool → args object (args may be {}; unknown keys refuse); "
            "absent means the model chooses calls and arguments within the trusted mask"
        ),
    )
    run_p.add_argument(
        "--allowed-tool-ids",
        type=json.loads,
        help=(
            "Optional trusted mask over the bundle bindings: JSON array of exact "
            "server:tool ids (subset of the pinned bundle; [] denies all). Absent "
            "means every trusted binding"
        ),
    )
    run_p.add_argument(
        "--coder-backend",
        type=str,
        choices=("bwrap", "local-python", "remote-openhands"),
        help=(
            "Explicit coder execution backend for code-execution tasks (tools_hint "
            "'coder'): 'bwrap' (Linux/CI hard sandbox), 'local-python' (macOS/dev, "
            "no net/fs isolation) or 'remote-openhands' (legacy OpenHands service; "
            "task content is model-generated — --coder-remote-url required, "
            "--coder-jobs not consulted). This flag is the operator's explicit allow "
            "for the write-effect coder tool; absent → such tasks typed-refuse"
        ),
    )
    run_p.add_argument(
        "--coder-remote-url",
        type=str,
        default=None,
        help=(
            "Required with --coder-backend remote-openhands: base URL of the legacy "
            "OpenHands task service (e.g. http://10.32.2.2:8884). No default endpoint "
            "is ever invented"
        ),
    )
    run_p.add_argument(
        "--coder-remote-header",
        type=str,
        action="append",
        default=None,
        metavar="NAME:VALUE",
        help=(
            "Optional (repeatable) with --coder-backend remote-openhands: extra HTTP "
            "header for the service, e.g. 'Authorization: Bearer ...'. Values are "
            "never written to the journal or receipts"
        ),
    )
    run_p.add_argument(
        "--coder-jobs",
        type=Path,
        help=(
            "Required with --coder-backend bwrap|local-python when the plan has "
            "code-execution tasks: JSON object task_key → {argv: [..], inputs: "
            "{name: str}, env: {..}, outputs: [{name, source: stdout|stderr, "
            "media_type}], limits: {timeout_s, memory_mb, max_output_bytes, "
            "pids_max, rlimit_nproc, allow_network, allow_cgroup_degraded, "
            "reap_timeout_s, output_drain_join_s}}. argv/limits are never invented "
            "by the CLI. Not valid with remote-openhands (content is model-generated)"
        ),
    )
    run_p.add_argument(
        "--wired-roles",
        type=str,
        default="[]",
        help=(
            "JSON array of optional roles to attempt when planner Need is on: "
            '["hypothesizer","researcher","report"] plus the wired lanes '
            '["critic","reviewer"] (default [] = fail-closed / cancel)'
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
    cont_p = sub.add_parser(
        "continue",
        help="Reopen an existing study journal and resume it to closure",
        epilog=_RC_EPILOG + "continue adds: 3 journaled-catalog/spec/stage-task mismatch,\n"
        "  5 live predecessor lease past --lease-wait-s (fail-closed)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cont_p.add_argument(
        "data_root",
        type=Path,
        help="Data root of the existing run (journal at <data-root>/<study-id>/events.db)",
    )
    cont_p.add_argument("--study-id", type=str, required=True)
    cont_p.add_argument("--catalog", type=Path, required=True, help="Catalog JSON path")
    cont_p.add_argument("--catalog-version", type=int, required=True)
    cont_p.add_argument("--profile", type=str, required=True)
    cont_p.add_argument("--tool-catalog-version", type=int, required=True)
    cont_p.add_argument("--owner", type=str, required=True)
    cont_p.add_argument("--llm-config", type=Path, required=True, help="LlmConfig JSON")
    cont_p.add_argument("--planner-model", type=str, required=True)
    cont_p.add_argument("--executor-model", type=str, required=True)
    cont_p.add_argument("--tz-spec-model", type=str, required=True)
    cont_tools_group = cont_p.add_mutually_exclusive_group(required=True)
    cont_tools_group.add_argument(
        "--tools-json",
        type=Path,
        help="Cassette path (same format as run): tool_name → {result, args}",
    )
    cont_tools_group.add_argument("--mcp-tool-bundle", type=Path)
    cont_p.add_argument("--tool-call-args", type=Path)
    cont_p.add_argument("--allowed-tool-ids", type=json.loads)
    cont_p.add_argument("--coder-backend", type=str, choices=("bwrap", "local-python", "remote-openhands"))
    cont_p.add_argument("--coder-jobs", type=Path)
    cont_p.add_argument(
        "--lease-ttl-s",
        type=float,
        required=True,
        help="Lease TTL for this continue session (takeover waits for the old lease)",
    )
    cont_p.add_argument(
        "--lease-wait-s",
        type=float,
        required=True,
        help=(
            "Bounded wait for the predecessor's lease to expire before refusing "
            "(0 = refuse immediately when held; fail-closed vs a live writer)"
        ),
    )
    cont_p.add_argument("--rng-seed", type=int, required=True)
    cont_p.add_argument("--max-llm-calls", type=int, required=True)
    cont_p.add_argument("--max-tokens", type=int, required=True)
    cont_p.add_argument("--max-tool-calls", type=int, required=True)
    cont_p.add_argument("--max-ticks", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "continue":
        try:
            return asyncio.run(_async_continue(args))
        except CliRefuse as exc:
            sys.stderr.write(f"cli refuse: {exc.detail}\n")
            raise SystemExit(exc.code) from None
    if args.command != "run":
        parser.error(f"unknown command {args.command!r}")
    try:
        return asyncio.run(_async_main(args))
    except CliRefuse as exc:
        sys.stderr.write(f"cli refuse: {exc.detail}\n")
        raise SystemExit(exc.code) from None


async def _async_main(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    control: StudyControl | None = None,
    runtime_opened: Callable[[str], None] | None = None,
) -> int:
    """Run the whole CLI ``run`` path for ``args``.

    ``out``: run-study summary sink; ``None`` → process stdout. The API web
    lane (``coscientist.api.run``) reuses this exact function — loaders,
    fail-closed validation, and ``run_study`` stay single-source in the CLI.
    """
    sink = out
    if sink is None:
        sink = sys.stdout
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
        _assert_profile_tools_on_surface(loaded_profile, tools, wired_roles=wired_roles)
        wired_models, wired_prompts = _wired_role_adapters(loaded_profile, wired_roles)
        adapter = LlmAgentAdapter(
            client,
            role_models={**role_models, **wired_models},
            tool_router=tool_router,
            prompts=role_prompt_registry(),
            role_prompts={
                "planner": ROLE_PLANNER_REF,
                "executor-route": ROLE_EXECUTOR_REF,
                "tz_spec": ROLE_TZ_SPEC_REF,
                "critic": ROLE_CRITIC_REF,
                "reviewer": ROLE_REVIEWER_REF,
                **wired_prompts,
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
            out=sink,
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
            remote_coder=_remote_coder_settings_from_args(args),
            llm_client=client,
            control=control,
            runtime_opened=runtime_opened,
        )
    finally:
        for live in live_to_close:
            await live.aclose()


async def _async_continue(args: argparse.Namespace) -> int:
    """``coscientist continue``: reopen + resume (see ``continue_cmd`` design)."""
    from coscientist.cli.continue_cmd import continue_study

    _validate_tool_cli_flags(args)
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
    study_clock = SystemClock()
    live_to_close: list[ToolRouter] = []
    try:
        tools, tool_router = await _open_tool_surface(args)
        if args.mcp_tool_bundle is not None:
            live_to_close.append(tool_router)
        _assert_profile_tools_on_surface(loaded_profile, tools)

        def _adapter_for(hitl_policy: HitlPolicy) -> LlmAgentAdapter:
            """Adapter built with the JOURNALED study HitlPolicy (no CLI drift)."""
            client = LlmClient(
                llm_config,
                HttpxTransport(),
                clock=llm_clock,
                sleep=wall_sleep,
                wait_for=wall_wait_for,
                rng=random.Random(args.rng_seed),
            )
            return LlmAgentAdapter(
                client,
                role_models=role_models,
                tool_router=tool_router,
                prompts=role_prompt_registry(),
                role_prompts={
                    "planner": ROLE_PLANNER_REF,
                    "executor-route": ROLE_EXECUTOR_REF,
                    "tz_spec": ROLE_TZ_SPEC_REF,
                    "critic": ROLE_CRITIC_REF,
                    "reviewer": ROLE_REVIEWER_REF,
                },
                clock=study_clock,
                hitl_policy=hitl_policy,
            )

        return await continue_study(
            data_root=args.data_root,
            study_id=args.study_id,
            catalog=catalog,
            loaded_profile=loaded_profile,
            owner=args.owner,
            adapter_factory=_adapter_for,
            out=sys.stdout,
            catalog_version=args.catalog_version,
            tools=tools,
            lease_ttl_s=args.lease_ttl_s,
            lease_wait_s=args.lease_wait_s,
            max_llm_calls=args.max_llm_calls,
            max_tokens=args.max_tokens,
            max_tool_calls=args.max_tool_calls,
            max_ticks=args.max_ticks,
            coder_backend=args.coder_backend,
            coder_jobs=coder_jobs,
            study_clock=study_clock,
        )
    finally:
        for live in live_to_close:
            await live.aclose()


def _validate_tool_cli_flags(args: argparse.Namespace) -> None:
    """Fail-closed tool-path flags before catalog/LLM I/O."""
    if args.tools_json is not None and args.tool_call_args is not None:
        raise CliRefuse("--tool-call-args is only valid with --mcp-tool-bundle", code=2)
    if args.tools_json is None and args.mcp_tool_bundle is None:
        raise CliRefuse("one of --tools-json or --mcp-tool-bundle is required", code=2)
    # Both ``run`` and ``continue`` parsers declare --coder-backend (default None).
    if args.coder_jobs is not None and args.coder_backend is None:
        raise CliRefuse(
            "--coder-jobs is only valid with --coder-backend (explicit backend required)",
            code=2,
        )
    if args.coder_jobs is not None and args.coder_backend == "remote-openhands":
        raise CliRefuse(
            "--coder-jobs is not valid with remote-openhands (task content is "
            "model-generated; docs/remote-coder-runtime.md §2)",
            code=2,
        )
    _remote_coder_settings_from_args(args)


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
    remote_coder: RemoteCoderSettings | None = None,
    llm_client: LlmClient | None = None,
    control: StudyControl | None = None,
    runtime_opened: Callable[[str], None] | None = None,
) -> int:
    """Create study, run stages → plan → experiments via ``Executor.record``, tick to close."""
    if not isinstance(loaded_profile, Profile):
        raise CliRefuse(
            f"loaded_profile must be Profile, got {type(loaded_profile).__name__}",
            code=2,
        )
    _assert_profile_cli_routes(loaded_profile)
    _assert_profile_tools_on_surface(loaded_profile, tools, wired_roles=wired_roles)
    # Wired need-stage deps (fail-closed before any study work; single-source
    # role+route validation with the adapter-building path): profile role +
    # matching route, the adapter output schema, and exact LIT surface ids
    # (literature tools resolve; no blanket-allow).
    _wired_role_adapters(loaded_profile, wired_roles)
    for wired_role in _WIRED_STAGE_ROUTES:
        if wired_role in wired_roles and not LlmAgentAdapter.supports_role(wired_role):
            raise CliRefuse(
                f"{wired_role} declared in --wired-roles but LlmAgentAdapter has no "
                "output schema; hard dep: adapter role + catalog/profile (#65) / "
                "sandbox (#150) — do not stub science",
                code=5,
            )
    lit_tool_ids: tuple[str, ...] = ()
    if _ROLE_LIT in wired_roles:
        lit_tools = loaded_profile.roles[_ROLE_LIT].tools
        lit_tool_ids = _resolve_profile_tools_on_surface(tools.tool_names, lit_tools, role=_ROLE_LIT)
    profile_name = loaded_profile.name
    if require_hypotheses and _ROLE_HYP not in wired_roles:
        raise CliRefuse(
            "require_hypotheses=true needs hypothesizer in --wired-roles "
            "(and LlmAgentAdapter schema; hard dep until adapter wires the role)",
            code=5,
        )
    # Report lane deps (fail-closed before any study work): the post-finalize
    # lane needs the LlmClient and a profile report role (model; no fallback).
    report_lane: tuple[LlmClient, str] | None
    report_lane = None
    if _ROLE_REPORT in wired_roles:
        if llm_client is None:
            raise CliRefuse(
                "report declared in --wired-roles but no LlmClient was passed to run_study (report lane hard dep)",
                code=5,
            )
        report_role = loaded_profile.roles.get(_ROLE_REPORT)
        if report_role is None:
            raise CliRefuse(
                f"profile {loaded_profile.name!r} has no report role; --wired-roles "
                f"report needs profile.roles[report].model (no invented fallback)",
                code=5,
            )
        report_lane = (llm_client, report_role.model)
    # Critic / reviewer deps (same fail-closed pattern as the report lane,
    # before any study work): both roles run through the adapter, so the hard
    # deps are the adapter instance (role model + prompt registered) and a
    # profile role (model; no fallback). No llm_client seam here — the adapter
    # carries the client (unlike the report lane's direct-client documents).
    if _ROLE_CRITIC in wired_roles:
        if not adapter.can_run_role(_ROLE_CRITIC):
            raise CliRefuse(
                "critic declared in --wired-roles but LlmAgentAdapter cannot run the "
                "role (no role model/prompt registered for it); hard dep — do not "
                "stub science",
                code=5,
            )
        if loaded_profile.roles.get(_ROLE_CRITIC) is None:
            raise CliRefuse(
                f"profile {loaded_profile.name!r} has no critic role; --wired-roles "
                f"critic needs profile.roles[critic].model (no invented fallback)",
                code=5,
            )
    if _ROLE_REVIEWER in wired_roles:
        if not adapter.can_run_role(_ROLE_REVIEWER):
            raise CliRefuse(
                "reviewer declared in --wired-roles but LlmAgentAdapter cannot run the "
                "role (no role model/prompt registered for it); hard dep — do not "
                "stub science",
                code=5,
            )
        if loaded_profile.roles.get(_ROLE_REVIEWER) is None:
            raise CliRefuse(
                f"profile {loaded_profile.name!r} has no reviewer role; --wired-roles "
                f"reviewer needs profile.roles[reviewer].model (no invented fallback)",
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
    usages: list[dict[str, object]] = []
    remote_runtime: RemoteCoderRuntime | None = None  # audit-ok[field_default]: composed only for remote-openhands
    if coder_backend is not None and resolve_coder_backend(coder_backend) == "remote-openhands":
        if remote_coder is None:
            raise CliRefuse(
                "remote-openhands backend requires remote coder settings "
                "(--coder-remote-url); refuse",
                code=2,
            )
        remote_runtime = compose_remote_coder_runtime(
            remote_coder,
            store_root=data_root / study_id / "coder-remote",
            study_deadline=deadline,
            clock=wall,
        )

    # Cumulative session budget from CLI flags (preserved); profile routes validated above.
    role_budget = Budget(
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_cost_rub=None,
        max_tool_calls=max_tool_calls,
        deadline=deadline,
    )

    rt: StudyRuntime | None
    rt = None
    try:
        if not isinstance(hitl_policy, HitlPolicy):
            raise CliRefuse(
                f"hitl_policy must be HitlPolicy, got {type(hitl_policy).__name__}",
                code=2,
            )
        # Bounded replan (adapter repair pattern at plan level): a derive or
        # accept_plan refusal feeds its detail back through
        # StudyContext.plan_refusal_hint for exactly ONE second planner round.
        # Derive refusals do not finalize → replan on the SAME study.
        # accept_plan refusals finalize the study REFUSED (core fail-closed) →
        # the replan round opens ONE fresh study ``<study_id>-r1`` and records
        # the SAME validated SPEC output verbatim (one tz_spec call per run,
        # no fabricated plan). A second refusal is terminal (rc=3);
        # max_expansions is untouched (plan-level repair is not expansion).
        spec_output: ResearchSpec | None
        spec_output = None
        refusal_hint = ""
        replanned = False
        study_attempt = 0
        while True:
            study_attempt += 1
            if study_attempt == 1:
                active_sid = study_id
            else:
                active_sid = f"{study_id}-r{study_attempt - 1}"
            runtime_cls = StudyRuntime
            if control is not None:
                runtime_cls = control.runtime_cls
            if rt is not None:
                await close_runtime(rt)
                rt = None
            if isinstance(tools, StaticToolRouter):
                register_trusted_mcp_tools(tools.config)
            opened = await open_runtime(
                data_root,
                active_sid,
                catalog=catalog,
                clock=wall,
                owner=owner,
                runtime_cls=runtime_cls,
            )
            if not isinstance(opened, StudyRuntime):
                raise CliRefuse(
                    f"open_runtime returned {type(opened).__name__}, expected StudyRuntime",
                    code=4,
                )
            rt = opened
            if isinstance(adapter, LlmAgentAdapter):
                if isinstance(tools, LiveCliToolSurface):
                    adapter.bind_tool_executor(RuntimeMcpExecutor(rt, tools.router, tools.tool_names))
                elif isinstance(tools, ToolRouter):
                    adapter.bind_tool_executor(RuntimeMcpExecutor(rt, tools, tools.tool_names))
            if runtime_opened is not None:
                runtime_opened(active_sid)
            writer_job_journal = _WriterJobJournal(rt.writer)
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
                    sid=active_sid,
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

            satisfiable_templates = _satisfiable_criteria_templates(catalog, allowed_tool_names)
            # RC4 readable pin: with the hypotheses stage required, the accept
            # gate additionally demands ≥1 HYP-n task. State that contract in
            # the payload (fact + consequence) so the planner can satisfy it
            # while planning (live st_idem_1ea2b5c4…: needed=true but zero HYP
            # tasks — the rule existed only inside the validator). Empty when
            # the stage is not required; accept_plan stays the honest backstop.
            hypotheses_stage_pin = ""
            if require_hypotheses:
                hypotheses_stage_pin = HYPOTHESES_STAGE_REQUIRED_PIN
            ctx = StudyContext(
                study_id=active_sid,
                request=request_text,
                profile=profile_name,
                # Profile hypothesis requirement as planner-visible context:
                # the contract rides the user payload (never a prompt patch);
                # the downstream profile gate stays the honest typed refusal.
                require_hypotheses=require_hypotheses,
                hypotheses_stage_pin=hypotheses_stage_pin,
                criterion_template_ids=tuple(sorted(t.id for t in satisfiable_templates)),
                # Templates whose check pins a concrete output: the planner request
                # schema refuses citing them on tasks that never declare that output
                # (same contract derive enforces, raised at parse for cheap repair).
                criterion_output_pins=tuple(
                    sorted(
                        (
                            CriterionOutputPin(criterion=t.id, requires_output=t.check.output)
                            for t in satisfiable_templates
                            if isinstance(t.check, CodeCheck) and t.check.output
                        ),
                        key=lambda pin: (pin.criterion, pin.requires_output),
                    )
                ),
                tool_ids=tuple(sorted(allowed_tool_names)),
                # Registered study inputs: the planner plans against names + refs
                # (never prompt-path guessing).
                inputs=registered_inputs,
            )
            if refusal_hint:
                ctx = ctx.model_copy(update={"plan_refusal_hint": refusal_hint})

            # SPEC — tz_spec agent → Executor.record. Called once per run; the
            # replan study records the SAME validated output on its own SPEC
            # attempt (same request → same spec; no second model call).
            spec_aid = await _require_attempt(rt, "SPEC")
            fresh_spec = spec_output is None
            if spec_output is None:
                while True:
                    try:
                        spec_run = await adapter.run("tz_spec", ctx, [], role_budget)
                        break
                    except LlmError as exc:
                        # A transport-level LLM failure is ATTEMPT-level, not
                        # run-fatal (same class as schema-invalid output):
                        # close the SPEC attempt retryable via the core and
                        # re-drive the role call on a fresh attempt. Core
                        # exhaustion → honest typed refuse (rc=4).
                        if not await _fail_stage_attempt_retryable(
                            rt,
                            spec_aid,
                            role="tz_spec",
                            task_key="SPEC",
                            detail=f"llm error: {exc}",
                            usages=usages,
                        ):
                            raise CliRefuse(
                                f"tz_spec llm error after the SPEC attempt budget was exhausted: {exc}",
                                code=4,
                            ) from exc
                        spec_aid = await _require_attempt(rt, "SPEC")
                usages.append(_usage_row("tz_spec", spec_run))
                if spec_run.error is not None:
                    raise CliRefuse(f"tz_spec error: {spec_run.error}", code=4)
                if not isinstance(spec_run.output, ResearchSpec):
                    raise CliRefuse("tz_spec must return ResearchSpec", code=4)
                spec_output = spec_run.output
            if control is not None and fresh_spec:
                # HITL v2 §5 gate 1: spec_approval after SPEC success, before
                # PLAN. Gate + one repair round run BEFORE the journal record
                # so the accepted output is the one that closes the SPEC
                # attempt (same attempt, new messages — no attempt-budget burn).
                spec_output = (
                    await _stage_gate_review(
                        rt, control,
                        kind="spec_approval", task_key="SPEC", attempt_id=spec_aid,
                        question="Принять это ТЗ (спецификацию исследования)?",
                        output=spec_output,
                        snapshot=lambda spec: spec.model_dump(mode="json"),
                        run_repair=_ctx_role_repair(
                            adapter, role="tz_spec", ctx=ctx, role_budget=role_budget,
                            usages=usages, output_cls=ResearchSpec,
                        ),
                        usages=usages,
                        gate_label="spec_approval gate",
                    )
                )[0]
            await record_research_spec(rt, spec_aid, spec_output)

            # Planner before HYP/LIT close so Need is known (no fabricated
            # SUCCESS). Inner loop = planner rounds on THIS runtime: derive
            # refusals do not finalize the study, so their replan round runs
            # in place on the same journal (no reopen, no dangling study).
            # The wired critic shares this ONE in-place round: critic issues
            # feed back through plan_refusal_hint for exactly one second
            # planner round (the #219 machinery — no second replan path), so
            # the critic stage is structurally bounded to ≤2 LLM calls per
            # planning cycle; the session Budget.max_llm_calls caps the total.
            while True:
                derive_refusal = ""
                try:
                    plan_out = await _plan_round(
                        rt,
                        adapter=adapter,
                        ctx=ctx,
                        role_budget=role_budget,
                        usages=usages,
                        wired_roles=wired_roles,
                        catalog=catalog,
                        allowed_tool_names=allowed_tool_names,
                        tools=tools,
                        study_id=active_sid,
                        registered_inputs=registered_inputs,
                        lit_tool_ids=lit_tool_ids,
                        control=control,
                    )
                except PlanDeriveError as exc:
                    if replanned:
                        raise CliRefuse(
                            f"plan derivation refused (after one replan round): {exc.detail}",
                            code=3,
                        ) from exc
                    derive_refusal = f"plan derivation refused: {exc.detail}"
                except AgentError as exc:
                    # Planner output that cannot be parsed as PlanIntent (schema
                    # validation, including the targeted repair round) gets the
                    # SAME single in-place replan round as a derive refusal: a
                    # fresh planner call with the exact validation error as
                    # plan_refusal_hint. Any other adapter failure propagates
                    # unchanged (executor/HYP/LIT failures below raise CliRefuse,
                    # not AgentError, so they never enter this branch).
                    if replanned or not _is_planner_schema_refusal(exc.detail):
                        raise
                    derive_refusal = f"planner schema refused: {exc.detail}"
                if derive_refusal:
                    replanned = True
                    refusal_hint = derive_refusal
                    ctx = ctx.model_copy(update={"plan_refusal_hint": refusal_hint})
                    continue
                # Wired plan critic (DESIGN §7.1): derived plan + context →
                # CriticVerdict, BEFORE accept_plan. accept → proceed as today;
                # revise/reject with the one replan round still unspent → one
                # planner retry with the issues as plan_refusal_hint; issues
                # after that round → the plan is accepted WITH the issues
                # journaled in the run summary (fail-closed = never silently
                # dropped; the plan-approval HITL gate stays the hard gate).
                if _ROLE_CRITIC in wired_roles:
                    critic_run = await adapter.run("critic", CriticIn(context=ctx, plan=plan_out), [], role_budget)
                    usages.append(_usage_row("critic", critic_run))
                    if critic_run.error is not None:
                        raise CliRefuse(f"critic error: {critic_run.error}", code=4)
                    verdict = critic_run.output
                    if not isinstance(verdict, CriticVerdict):
                        raise CliRefuse("critic must return CriticVerdict", code=4)
                    if verdict.decision != "accept":
                        issue_rows = [f"{i.path}: {i.message}" for i in verdict.issues]
                        if replanned:
                            usages.append(
                                {
                                    "role": "critic",
                                    "stage": "plan",
                                    "decision": verdict.decision,
                                    "issues": issue_rows,
                                    "note": (
                                        "critic issues persist after the one bounded replan "
                                        "round; plan accepted with issues journaled (never silent)"
                                    ),
                                }
                            )
                        else:
                            replanned = True
                            refusal_hint = "critic issues: " + "; ".join(issue_rows)
                            ctx = ctx.model_copy(update={"plan_refusal_hint": refusal_hint})
                            continue
                break
            if control is not None:
                await _require_attempt(rt, "PLAN")
                if not await control.review_plan(rt, plan_out):
                    raise CliRefuse("human plan approval refused or expired", code=3)
            plan_id = await rt.accept_plan(
                tasks=[_to_task_spec(t) for t in plan_out.tasks],
                questions=[
                    QuestionSpec(
                        id=q.id,
                        text=q.text,
                        final_type=q.final_type,
                        claim_class=q.claim_class,
                        acceptance_route=q.acceptance_route,
                    )
                    for q in plan_out.questions
                ],
                # Main requires these keyword args (no invent / no Optional defaults).
                subquestions=[],
                spec_output=None,
                expand_after=[],
                loops=[],
                hypotheses=plan_out.hypotheses,
                literature=plan_out.literature,
                # Derive already dropped the uncitable pin-mismatch criteria;
                # accept journals them (readable system note, never silent).
                derive_uncitable_drops=plan_out.uncitable_drops,
            )
            if plan_id:
                break
            detail = await _plan_refused_detail(rt)
            if detail:
                refusal_hint = f"accept_plan refused plan: {detail}"
            else:
                refusal_hint = "accept_plan refused plan: empty plan_id (no PlanRefused detail)"
            if replanned:
                raise CliRefuse(f"{refusal_hint} (after one replan round)", code=3)
            replanned = True
            # accept_plan refusal finalized this study REFUSED → the next loop
            # iteration opens the single replan study with the hint fed back.

        # REPORT DAG placeholder: unwired → official cancel (CLI behavior
        # unchanged); wired → close the placeholder too — the real reporting
        # runs post-finalize (full graph; I3 closes journal writes), see the
        # report lane call after the tick loop. HITL v2 §5 gate 5: with a live
        # control AND a report lane the placeholder stays pending as the
        # finalize barrier — the gate flow closes it right before finalize.
        report_gate_live = control is not None and report_lane is not None
        await _optional_report_stage(
            rt, wired_roles=wired_roles, keep_open=report_gate_live,
        )

        # Bounded recovery loop (gaps №6/7): dispatch → tick → dispatch …
        # Retries are the CORE's own machinery, never a CLI-side loop:
        # a transient executor failure closes the attempt retryable via
        # ``rt.fail_attempt`` (AttemptClosed.retryable + retry_at), and the
        # tick's ``_fire_due_retries`` starts the next attempt under the
        # Study.max_attempts / task-deadline / budget bounds the core already
        # enforces. This driver only (a) starts FIRST attempts for dispatchable
        # tasks, (b) executes each open attempt exactly once, (c) ticks until
        # the study closes. Loop bounds: max_ticks cap below + the core's
        # deadline/budget/attempt gates — no unbounded spin is possible.
        executed_aids: set[str] = set()
        coder_dispatched: set[str] = set()
        ticks = 0
        report_gated = False
        while rt.view().phase != "closed":
            if ticks >= max_ticks:
                raise CliRefuse(
                    f"max_ticks={max_ticks} exhausted before phase=closed "
                    f"(phase={rt.view().phase!r}, verdict={rt.view().verdict!r})",
                    code=6,
                )
            # HITL v2 report gate: REPORT is the last pending task → draft,
            # human gate, close REPORT; the next tick finalizes (§5 «до
            # StudyFinalized»). Lane refusal falls back to the legacy lane.
            if report_gate_live and not report_gated and _report_gate_due(rt):
                assert control is not None
                assert report_lane is not None
                report_gated = await _interactive_report_gate(
                    rt,
                    control=control,
                    lane=report_lane,
                    role_budget=role_budget,
                    wall=wall,
                    usages=usages,
                )
                continue
            # Shared terminal-cause preflight (adaptive runtime): the core's
            # monetary producer (folded BudgetDebited → BudgetExceeded) plus
            # the SAME trigger priority the tick seals with decide whether
            # more work may start. A pending cancel/budget/intake/deadline
            # trigger skips dispatch — no work starts after the deadline or
            # funded-budget exhaustion, and the seal keeps the authoritative
            # cause (never an all_done seal that hides deadline/budget).
            if await rt.dispatch_preflight() is None:
                await _drive_plan_attempts(
                    rt,
                    plan_out=plan_out,
                    tools=tools,
                    adapter=adapter,
                    study_id=active_sid,
                    registered_inputs=registered_inputs,
                    role_budget=role_budget,
                    usages=usages,
                    ctx=ctx,
                    allowed_tool_names=allowed_tool_names,
                    executed_aids=executed_aids,
                    coder_dispatched=coder_dispatched,
                    coder_backend=coder_backend,
                    coder_jobs=coder_jobs,
                    writer_journal=writer_job_journal,
                    wall=wall,
                    hitl_policy=hitl_policy,
                    deadline=deadline,
                    remote_runtime=remote_runtime,
                    defer_report=report_gate_live and not report_gated,
                )
                await rt.ensure_lease(ttl_s=float(lease_ttl_s))
            await rt.tick()
            ticks += 1

        view = rt.view()
        if report_lane is not None and view.verdict == "fulfilled" and not report_gated:
            # Post-finalize report lane: scientific report + ГОСТ ТЗ/НИР from
            # the graph projection into the CAS. Runs after close so the graph
            # is complete; writes no journal events — the verdict above is
            # already final and report content never flips it. The gated
            # interactive path already landed the final docs pre-finalize
            # (human feedback folded in) — no second render.
            lane_client, lane_model = report_lane
            try:
                await run_report_lane(
                    rt,
                    client=lane_client,
                    model_id=lane_model,
                    budget=role_budget,
                    clock=wall,
                    usages=usages,
                )
            except LlmError as exc:
                # The verdict above is already final; a lane transport failure
                # keeps the existing rc=8 lane-refusal semantics (typed, study
                # stays terminal) instead of a raw crash past finalize.
                raise CliRefuse(
                    f"report lane refused post-finalize: llm error: {exc}", code=8
                ) from exc
        if _ROLE_REVIEWER in wired_roles and view.verdict == "fulfilled":
            # Post-finalize review lane (same pattern): the wired reviewer runs
            # through the adapter over the graph digest → CAS review.md + a
            # usage row. Advisory by construction: no journal events, so the
            # recorded verdict can never flip. Fulfilled-only mirrors the
            # report lane (a non-fulfilled study exits rc=7 unshadowed).
            await run_review_lane(
                rt,
                adapter=adapter,
                ctx=ctx,
                budget=role_budget,
                usages=usages,
            )
        out.write(
            json.dumps(
                {
                    # The study that actually closed (the replan study when the
                    # first plan was refused); usage accumulates every round.
                    "study_id": active_sid,
                    "phase": view.phase,
                    "verdict": view.verdict,
                    # Observed quality of the closing study (bench harness reads
                    # this; see _observed_quality for the exact fold).
                    "quality": _observed_quality(rt),
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
    except LlmError as exc:
        # Backstop for role calls OUTSIDE any task attempt (planner round,
        # plan critic, post-finalize reviewer lane): an LLM transport failure
        # must surface as an honest typed refuse, never escape run_study as a
        # raw crash (wave-6: st_idem_5400 / st_idem_4d89 died run-level on
        # LlmTimeoutError). Attempts close retryable in their own lanes above.
        raise CliRefuse(f"llm error: {exc}", code=4) from exc
    finally:
        if rt is not None:
            await close_runtime(rt)


_StageOut = TypeVar("_StageOut")


def _ctx_role_repair(
    adapter: LlmAgentAdapter,
    *,
    role: str,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    output_cls: type[Any],
) -> Callable[[str], Awaitable[Any]]:
    """Repair callback for ctx-driven stage roles (tz_spec / hypothesizer):
    re-runs the SAME role with the human's exact feedback (HITL v2 §5; the
    adapter renders it as the repair user message; same attempt, no burn)."""

    async def _run(feedback: str) -> Any:
        repair_run = await adapter.run(role, ctx, [], role_budget, repair_feedback=feedback)
        usages.append(_usage_row(role, repair_run))
        if repair_run.error is not None:
            raise CliRefuse(f"{role} error: {repair_run.error}", code=4)
        if not isinstance(repair_run.output, output_cls):
            raise CliRefuse(f"{role} must return {output_cls.__name__}", code=4)
        return repair_run.output

    return _run


async def _journal_feedback_rows(rt: StudyRuntime) -> list[dict[str, str]]:
    """Human gate-feedback rows folded from the journal (report comments section)."""
    return human_gate_feedback(await rt.store.read_all())


async def _stage_gate_review(
    rt: StudyRuntime,
    control: StudyControl,
    *,
    kind: str,
    task_key: str,
    attempt_id: str,
    question: str,
    output: _StageOut,
    snapshot: Callable[[_StageOut], dict[str, object]],
    run_repair: Callable[[str], Awaitable[_StageOut]],
    usages: list[dict[str, object]],
    gate_label: str,
) -> tuple[_StageOut, dict[str, object]]:
    """One interactive stage gate with ONE bounded repair round (hitl-map §5).

    ``revise`` OR ``reject``-with-feedback re-drives the SAME stage attempt
    through ``run_repair`` with the human's exact text (the adapter renders it
    as the repair user message); the corrected output opens a fresh gate
    round. A second return, or a bare reject without repair material, cancels
    the study and refuses typed — naming the gate and the human feedback
    (owner law: honest typed refusal, never silent death).
    """
    outcome = await control.review_stage(
        rt, kind=kind, task_key=task_key, attempt_id=attempt_id,
        question=question, snapshot=snapshot(output),
    )
    usages.append({
        "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
        "decision": outcome.decision, "feedback": outcome.feedback,
    })
    repaired = False
    while outcome.decision in ("revise", "reject"):
        feedback = outcome.feedback
        if feedback is None:
            await rt.cancel_study(by=f"human-{kind}-rejected")
            raise CliRefuse(f"{gate_label}: human rejected without repair feedback", code=3)
        if repaired:
            await rt.cancel_study(by=f"human-{kind}-rejected")
            raise CliRefuse(
                f"{gate_label}: human repair rounds exhausted; feedback: {feedback}",
                code=3,
            )
        repaired = True
        usages.append({
            "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
            "note": "one bounded repair round re-drives the stage role with the human feedback",
        })
        try:
            output = await run_repair(feedback)
        except LlmError as exc:
            # The repair role call failed at the LLM transport level inside
            # the SAME stage attempt: close the attempt retryable via the
            # core, then refuse typed with the human feedback preserved in
            # the detail (owner law — honest refusal, never a raw crash; the
            # gate flow must not silently re-ask past a human touchpoint).
            await rt.fail_attempt(attempt_id, retryable=True, close_reason=CloseReason.FAILED)
            usages.append({
                "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
                "attempt_id": attempt_id,
                "retryable_error": f"gate repair llm error: {exc}",
            })
            raise CliRefuse(
                f"{gate_label}: repair llm error with human feedback {feedback!r}: {exc}",
                code=4,
            ) from exc
        outcome = await control.review_stage(
            rt, kind=kind, task_key=task_key, attempt_id=attempt_id,
            question=question, snapshot=snapshot(output),
        )
        usages.append({
            "role": "hitl-stage-gate", "task_key": task_key, "kind": kind,
            "decision": outcome.decision, "feedback": outcome.feedback,
        })
    return output, {"kind": kind, "decision": outcome.decision, "feedback": outcome.feedback}


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
    need_label: str,
    tools: CliToolSurface,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    lit_tool_ids: tuple[str, ...],
    control: StudyControl | None = None,
) -> None:
    """Need-on + unwired → refuse; wired → honest run; need-off defers to accept.

    Wired path mirrors the SPEC stage: one start_attempt → one adapter run →
    journal record (``record_hypotheses`` / ``record_literature``) closes the
    DAG task non-SKIPPED. Runtime/agent failures refuse terminal (rc=4) —
    stage retries are not re-driven by the CLI loop (same exposure as SPEC).

    Terminal DAG state is written from the ACCEPTED plan only: need-off here
    does NOT cancel the stage (an intermediate round may flip need back on —
    the wired stage stays startable); the core's accept_plan closes not-needed
    HYP/LIT SKIPPED in the PlanAccepted transaction. A DONE SUCCESS stage
    (satisfied in an earlier round) is never re-driven — the journal record
    stands; a DONE non-SUCCESS stage on a needed=true flip is drift → typed
    refuse (TaskDone is terminal, the core has no reopen).
    """
    if not needed:
        usages.append(
            {
                "role": role,
                "task_key": task_key,
                "note": (
                    f"{need_label} not needed this round; closure defers to the "
                    "accepted plan (need stages stay startable until accept)"
                ),
            }
        )
        return
    stage = _task_by_key(rt, task_key)
    if stage is not None and stage.state == TaskState.DONE:
        if stage.outcome == TaskOutcome.SUCCESS:
            usages.append(
                {
                    "role": role,
                    "task_key": task_key,
                    "note": (
                        f"{need_label} already journaled in an earlier plan round; "
                        "need stages are one-shot — not re-driven"
                    ),
                }
            )
            return
        raise CliRefuse(
            f"planner set {need_label}.needed=true but the {task_key} stage closed as "
            f"{str(stage.outcome)} in an earlier plan round (need stages are one-shot; "
            "TaskDone is terminal — no reopen)",
            code=3,
        )
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
    if role == _ROLE_HYP:
        await _run_wired_hyp_stage(
            rt,
            adapter=adapter,
            ctx=ctx,
            role_budget=role_budget,
            usages=usages,
            task_key=task_key,
            control=control,
        )
        return
    if role == _ROLE_LIT:
        await _run_wired_lit_stage(
            rt,
            adapter=adapter,
            tools=tools,
            role_budget=role_budget,
            usages=usages,
            task_key=task_key,
            study_id=study_id,
            registered_inputs=registered_inputs,
            lit_tool_ids=lit_tool_ids,
            control=control,
        )
        return
    raise CliRefuse(f"role {role!r} has no wired need-stage runner (drift)", code=5)


async def _run_wired_hyp_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    control: StudyControl | None = None,
) -> None:
    """HYP stage via the hypothesizer role: StudyContext in, HypothesisOut journaled.

    HITL v2 §5 gate 2: with a live control the gate (and its one repair round)
    sits between the validated adapter output and the journal record, so the
    accepted output is the one that closes the HYP attempt (same attempt).
    """
    aid = await _require_attempt(rt, task_key)
    while True:
        try:
            hyp_run = await adapter.run(_ROLE_HYP, ctx, [], role_budget)
            break
        except LlmError as exc:
            # ATTEMPT-level failure (see the SPEC lane): retryable attempt
            # close via the core, fresh attempt re-drives the role call;
            # exhaustion → honest typed refuse (rc=4), never a run crash.
            if not await _fail_stage_attempt_retryable(
                rt,
                aid,
                role=_ROLE_HYP,
                task_key=task_key,
                detail=f"llm error: {exc}",
                usages=usages,
            ):
                raise CliRefuse(
                    f"hypothesizer llm error after the {task_key} attempt budget was exhausted: {exc}",
                    code=4,
                ) from exc
            aid = await _require_attempt(rt, task_key)
    usages.append(_usage_row(_ROLE_HYP, hyp_run, task_key=task_key))
    if hyp_run.error is not None:
        raise CliRefuse(f"hypothesizer error: {hyp_run.error}", code=4)
    if not isinstance(hyp_run.output, HypothesisOut):
        raise CliRefuse("hypothesizer must return HypothesisOut", code=4)
    output = hyp_run.output
    if control is not None:
        question = f"Принять гипотезы исследования ({len(output.hypotheses)} шт.)?"
        output = (
            await _stage_gate_review(
                rt, control,
                kind="hypotheses", task_key=task_key, attempt_id=aid,
                question=question,
                output=output,
                snapshot=lambda out: out.model_dump(mode="json"),
                run_repair=_ctx_role_repair(
                    adapter, role=_ROLE_HYP, ctx=ctx, role_budget=role_budget,
                    usages=usages, output_cls=HypothesisOut,
                ),
                usages=usages,
                gate_label="hypotheses gate",
            )
        )[0]
    await record_hypotheses(rt, aid, output)


async def _run_wired_lit_stage(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    tools: CliToolSurface,
    role_budget: Budget,
    usages: list[dict[str, object]],
    task_key: str,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    lit_tool_ids: tuple[str, ...],
    control: StudyControl | None = None,
) -> None:
    """LIT stage via the researcher role: planned tool calls → LiteratureOut.

    Tool calls go through the planned surface exactly like executor tasks
    (profile role tools resolved to exact surface ids; call args come from the
    cassette / --tool-call-args). ``record_literature`` refuses papers that do
    not trace to journaled tool results (no invented citations).

    HITL v2 §5 gate 4: with a live control the gate (and its one repair round,
    which re-runs the researcher with the human text and fresh tool receipts)
    sits before the journal record.
    """
    if not lit_tool_ids:
        raise CliRefuse(
            "researcher declared in --wired-roles but the profile researcher role "
            "declares no literature tools (fail-closed: LIT runs through tools, "
            "never model-invented papers)",
            code=5,
        )
    lit_draft = TaskSpecDraft(
        task_key=task_key,
        title=task_key,
        goal="literature search for the study request (wired researcher role)",
        route="literature",
        depends_on=(),
        addresses=(),
        tools_hint=lit_tool_ids,
        expected_outputs=(),
        criteria=(),
    )
    # Resolve planned call args BEFORE start_attempt (no dangling AttemptStarted).
    try:
        planned = tools.planned_calls(lit_tool_ids)
    except KeyError as exc:
        missing = exc.args[0]
        raise CliRefuse(
            f"LIT stage tools_hint missing declared call args for {missing!r} (fail-closed before start_attempt)",
            code=3,
        ) from exc
    aid = await _require_attempt(rt, task_key)
    study_request = ""
    if rt.study is not None:
        study_request = rt.study.request
    while True:
        # Rebuilt per attempt: the route input carries attempt_id, so a
        # retryable re-drive must bind tool receipts to the FRESH attempt
        # (stale ids would orphan the receipts the record path binds).
        lit_input: AutomaticExecutorIn | ExecutorRouteIn
        if isinstance(tools, LiveCliToolSurface) and tools.automatic:
            lit_input = AutomaticExecutorIn(
                mode="automatic",
                request=study_request,
                task=lit_draft,
                study_id=study_id,
                attempt_id=aid,
                inputs=tuple(registered_inputs),
            )
        else:
            lit_input = ExecutorRouteIn(
                task=lit_draft,
                study_id=study_id,
                attempt_id=aid,
                tool_args=planned,
                inputs=tuple(registered_inputs),
            )
        try:
            lit_run = await adapter.run(_ROLE_LIT, lit_input, list(tools.named_tools()), role_budget)
            break
        except LlmError as exc:
            # ATTEMPT-level failure (see the SPEC lane): a big-synthesis
            # researcher call that outlives the transport timeout closes the
            # LIT attempt retryable via the core; the fresh attempt re-runs
            # the planned tool calls + role call. Exhaustion → honest typed
            # refuse (rc=4), never the wave-6 run-crash (st_idem_4d89).
            if not await _fail_stage_attempt_retryable(
                rt,
                aid,
                role=_ROLE_LIT,
                task_key=task_key,
                detail=f"llm error: {exc}",
                usages=usages,
            ):
                raise CliRefuse(
                    f"researcher llm error after the {task_key} attempt budget was exhausted: {exc}",
                    code=4,
                ) from exc
            aid = await _require_attempt(rt, task_key)
    usages.append(_usage_row(_ROLE_LIT, lit_run, task_key=task_key))
    if lit_run.error is not None:
        raise CliRefuse(f"researcher error: {lit_run.error}", code=4)
    if not isinstance(lit_run.output, LiteratureOut):
        raise CliRefuse("researcher must return LiteratureOut", code=4)

    async def repair_lit(feedback: str) -> AgentRun:
        repair_run = await adapter.run(
            _ROLE_LIT, lit_input, list(tools.named_tools()), role_budget, repair_feedback=feedback,
        )
        usages.append(_usage_row(_ROLE_LIT, repair_run, task_key=task_key))
        if repair_run.error is not None:
            raise CliRefuse(f"researcher error: {repair_run.error}", code=4)
        if not isinstance(repair_run.output, LiteratureOut):
            raise CliRefuse("researcher must return LiteratureOut", code=4)
        return repair_run

    accepted_run = lit_run
    if control is not None:
        question = f"Принять найденную литературу ({len(lit_run.output.papers)} стат.)?"

        def lit_snapshot(run: AgentRun) -> dict[str, object]:
            if not isinstance(run.output, LiteratureOut):
                raise CliRefuse("researcher must return LiteratureOut", code=4)
            return run.output.model_dump(mode="json")

        accepted_run = (
            await _stage_gate_review(
                rt, control,
                kind="literature", task_key=task_key, attempt_id=aid,
                question=question,
                output=lit_run,
                snapshot=lit_snapshot,
                run_repair=repair_lit,
                usages=usages,
                gate_label="literature gate",
            )
        )[0]
    await record_literature(rt, aid, accepted_run)


async def _optional_report_stage(
    rt: StudyRuntime,
    *,
    wired_roles: frozenset[str],
    keep_open: bool = False,
) -> None:
    """Close the REPORT DAG placeholder; the wired lane runs post-finalize.

    HITL v2 §5 gate 5: the interactive report gate keeps the placeholder
    pending through the run loop — finalize needs all tasks DONE, so the gate
    sits before StudyFinalized by construction (finalize barrier).
    """
    if keep_open:
        return
    if _ROLE_REPORT not in wired_roles:
        await rt.cancel_task("REPORT", reason="cancelled", detail="report agent not wired")
        return
    # Wired: the placeholder still closes here (finalize needs all tasks DONE;
    # post-finalize the journal is closed by I3). The honest report lane then
    # renders from the completed graph projection into the run's CAS.
    await rt.cancel_task("REPORT", reason="cancelled", detail="report deferred to post-finalize lane")


def _report_gate_due(rt: StudyRuntime) -> bool:
    """True when REPORT is the only non-DONE task — the finalize barrier point."""
    report = _task_by_key(rt, "REPORT")
    if report is None or report.state != TaskState.PENDING:
        return False
    return all(
        task.state == TaskState.DONE
        for task in rt.tasks.values()
        if task.spec.task_key != "REPORT"
    )


async def _interactive_report_gate(
    rt: StudyRuntime,
    *,
    control: StudyControl,
    lane: tuple[LlmClient, str],
    role_budget: Budget,
    wall: Clock,
    usages: list[dict[str, object]],
) -> bool:
    """HITL v2 §5 gate 5: report draft → human gate → close REPORT → finalize.

    The REPORT placeholder is pending, so StudyFinalized cannot commit while
    the human reviews the draft (report must exist at any terminal — the
    closure below restores the legacy finalize path). Revise/reject with
    feedback re-renders the docs ONCE with the human text in the science
    payload; any second return finalizes anyway, with every gate feedback
    folded into the final «Замечания человека» section (journal facts).

    Returns True when the gate flow landed the final docs (the post-finalize
    lane must not re-render); False on lane refusal — the legacy post-finalize
    retry keeps its existing rc=8 semantics.
    """
    lane_client, lane_model = lane
    report = _task_by_key(rt, "REPORT")
    if report is None:
        raise CliRefuse("report gate: REPORT task missing (drift)", code=3)
    try:
        _rendered, digest, prose, markdown = await render_report_docs(
            rt, client=lane_client, model_id=lane_model, budget=role_budget, clock=wall,
            usages=usages, human_feedback=await _journal_feedback_rows(rt),
        )
    except CliRefuse as exc:
        # Pre-finalize lane refusal must not leave the study un-finalizable:
        # close the barrier typed; the legacy post-finalize lane retries after
        # finalize (existing rc=8 run refusal, study stays terminal).
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "note": f"report lane refused pre-finalize; finalize barrier closed typed: {exc.detail}",
        })
        await rt.cancel_task(
            "REPORT", reason="cancelled",
            detail=f"report lane refused pre-finalize: {exc.detail[:200]}",
        )
        return False  # audit-ok[except_silent_return]: typed fallback; barrier closed typed; legacy lane retries rc=8
    except LlmError as exc:
        # Same typed fallback for an LLM transport failure in the report lane
        # (big-synthesis documents legitimately outlive tight timeouts): the
        # finalize barrier closes typed, the legacy post-finalize lane retries
        # — no raw LlmTimeoutError may kill the run (wave-6 st_idem_5400).
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "note": f"report lane llm error pre-finalize; finalize barrier closed typed: llm error: {exc}",
        })
        await rt.cancel_task(
            "REPORT", reason="cancelled",
            detail=f"report lane llm error pre-finalize: {str(exc)[:200]}",
        )
        return False  # audit-ok[except_silent_return]: typed fallback; barrier closed typed; legacy lane retries rc=8
    outcome = await control.review_stage(
        rt, kind="report", task_key="REPORT", attempt_id=report.task_id,
        question="Принять научный отчёт исследования?",
        snapshot={"name": SCIENCE_ARTIFACT_NAME, "content": markdown},
    )
    usages.append({
        "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
        "decision": outcome.decision, "feedback": outcome.feedback,
    })
    repaired = False
    while outcome.decision in ("revise", "reject"):
        feedback = outcome.feedback
        if repaired or feedback is None:
            # Finalize anyway (owner law: report exists at any terminal); the
            # fold below already carries this gate's feedback from the journal.
            break
        repaired = True
        _rendered, digest, prose, markdown = await render_report_docs(
            rt, client=lane_client, model_id=lane_model, budget=role_budget, clock=wall,
            usages=usages, human_feedback=await _journal_feedback_rows(rt),
            science_note=feedback,
        )
        outcome = await control.review_stage(
            rt, kind="report", task_key="REPORT", attempt_id=report.task_id,
            question="Принять научный отчёт исследования?",
            snapshot={"name": SCIENCE_ARTIFACT_NAME, "content": markdown},
        )
        usages.append({
            "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
            "decision": outcome.decision, "feedback": outcome.feedback,
        })
    # Final science markdown re-assembles WITHOUT a new model call: the last
    # render's prose + the complete journal fold (includes this gate's own
    # accept/revise/reject feedback recorded by the owner before returning).
    final_markdown = build_science_report_markdown(
        digest, prose, human_feedback=await _journal_feedback_rows(rt),
    )
    study = rt.study
    if study is None:
        raise CliRefuse("report gate: runtime has no study", code=3)
    artifact_id = put_science_report(rt, study_id=study.study_id, markdown=final_markdown)
    usages.append({
        "role": "hitl-stage-gate", "task_key": "REPORT", "kind": "report",
        "note": "report gate closed; finalize proceeds",
        "decision": outcome.decision, "feedback": outcome.feedback,
        "science_artifact": artifact_id, "repair_rendered": repaired,
    })
    await rt.cancel_task(
        "REPORT", reason="cancelled",
        detail=f"report gate closed ({outcome.decision}); finalize proceeds",
    )
    return True


def _satisfiable_criteria_templates(
    catalog: Catalog, allowed_tool_names: frozenset[str]
) -> tuple[RequirementTemplate, ...]:
    """Catalogue templates the planner may honestly cite on THIS study surface.

    A template whose check pins a concrete output that no task can ever declare
    (a ``coder`` pin without the attached coder binding, or a pin naming no
    bound tool) cannot be cited without a guaranteed refusal — offering it in
    the criteria enum turns every planner round into one. Same satisfiable
    surface principle as the ``tools_hint`` enum.
    """
    return tuple(
        t
        for t in catalog.templates
        if not isinstance(t.check, CodeCheck)
        or not t.check.output
        or t.check.output in allowed_tool_names
        or (t.check.output == CODER_HINT and CODER_HINT in allowed_tool_names)
    )


def _is_planner_schema_refusal(detail: str) -> bool:
    """True only for a planner output that never parsed as PlanIntent.

    ``adapter.run`` raises ``AgentError`` whose detail is the schema
    validation failure after the targeted repair round. Transport/LLM
    failures must keep refusing immediately — only this class earns the
    plan-level replan round.
    """
    return "planner output failed schema validation" in detail


async def _plan_round(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    wired_roles: frozenset[str],
    catalog: Catalog,
    allowed_tool_names: frozenset[str],
    tools: CliToolSurface,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    lit_tool_ids: tuple[str, ...],
    control: StudyControl | None = None,
) -> PlanOut:
    """One planner call → derive → HYP/LIT need stages.

    Adapter-level failures refuse (rc=4, unchanged); ``PlanDeriveError`` is
    raised to the caller for the bounded replan-round decision.
    """
    plan_run = await adapter.run("planner", ctx, [], role_budget)
    usages.append(_usage_row("planner", plan_run))
    if plan_run.error is not None:
        raise CliRefuse(f"planner error: {plan_run.error}", code=4)
    intent_out = plan_run.output
    if not isinstance(intent_out, PlanIntent):
        raise CliRefuse("planner must return PlanIntent", code=4)
    plan_out = derive_plan(
        intent_out,
        catalog_templates=catalog.templates,
        tool_ids=sorted(allowed_tool_names),
    )
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
        need_label="hypotheses",
        tools=tools,
        study_id=study_id,
        registered_inputs=registered_inputs,
        lit_tool_ids=lit_tool_ids,
        control=control,
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
        need_label="literature",
        tools=tools,
        study_id=study_id,
        registered_inputs=registered_inputs,
        lit_tool_ids=lit_tool_ids,
        control=control,
    )
    return plan_out


def _task_by_key(rt: StudyRuntime, task_key: str) -> Task | None:
    """Resolve a plan task_key to its core ``Task`` row (None on drift)."""
    for task in rt.tasks.values():
        if task.spec.task_key == task_key:
            return task
    return None


def _resolve_planned_calls(tools: CliToolSurface, draft: TaskSpecDraft) -> tuple[PlannedToolCall, ...]:
    """Static per-draft validation: allow-listed hints + declared call args."""
    unknown = [h for h in draft.tools_hint if h not in tools.tool_names]
    if unknown:
        raise CliRefuse(
            f"task {draft.task_key!r} tools_hint {unknown!r} not in router allow-list",
            code=3,
        )
    # Resolve call args before start_attempt — KeyError must not leave AttemptStarted.
    try:
        return tools.planned_calls(draft.tools_hint)
    except KeyError as exc:
        # planned_calls always raises KeyError(hint) with one arg.
        missing = exc.args[0]
        raise CliRefuse(
            f"task {draft.task_key!r} tools_hint missing declared call args "
            f"for {missing!r} (fail-closed before start_attempt)",
            code=3,
        ) from exc


_PRIOR_FAILURE_DETAIL_CAP = 2000


def _prior_attempt_failure_reason(
    rt: StudyRuntime, usages: list[dict[str, object]], task_key: str, aid: str
) -> str | None:
    """Typed failure facts of this task's PREVIOUS attempt (None on attempt 1).

    Owner directive («КОГДА ЧТО-ТО ПАДАЕТ — РЕТРАИ С ВЫВЕДЕНИЕМ ПРИЧИНЫ…»):
    attempt-level retries must not be blind — the fresh attempt's role input
    carries WHY the previous one died. Deterministic and factual; the reason
    exists only when a prior attempt actually closed failed:

    - primary: the newest ``retryable_error`` usage row for this task key
      (the typed detail — tool/llm/record-contract error text the CLI wrote
      when it closed that attempt via the core ``fail_attempt`` path);
    - fallback (facts live per-process; a continue restart loses them): the
      journal's own ``close_reason`` on the task's last closed failed attempt
      row — less specific, still honest.

    Bounded: the reason string is capped (``_PRIOR_FAILURE_DETAIL_CAP``), so
    the facts message can never grow the prompt without bound.
    """
    for row in reversed(usages):
        if row.get("task_key") != task_key or not row.get("retryable_error"):
            continue
        role = str(row.get("role", "unknown"))  # audit-ok[dict_get_default]: CLI-internal rows always set role
        detail = str(row["retryable_error"])[:_PRIOR_FAILURE_DETAIL_CAP]
        return f"[{role}] {detail}"
    task = _task_by_key(rt, task_key)
    if task is None:
        return None
    for prior_aid in reversed(task.attempts):
        if prior_aid == aid:
            continue  # the attempt about to run, not a previous one
        att_row: Mapping[str, object] | None = rt.attempts.get(prior_aid)
        if not isinstance(att_row, Mapping) or att_row.get("closed") is not True:
            continue
        if att_row.get("close_reason") == CloseReason.FAILED.value:
            return f"close_reason={CloseReason.FAILED.value} (детали ошибки отсутствуют в этом запуске)"
    return None


async def _fail_attempt_retryable(
    rt: StudyRuntime,
    aid: str,
    *,
    task_key: str,
    detail: str,
    usages: list[dict[str, object]],
    role: str = "executor-route",
) -> None:
    """Close one executor attempt as a retryable failure via the CORE path.

    ``fail_attempt(retryable=True)`` journals ``AttemptClosed`` with
    ``retry_at`` and leaves the task PENDING; the tick loop's
    ``_fire_due_retries`` — not this CLI — starts the next attempt under the
    ``Study.max_attempts`` / task-deadline bounds. The error detail stays
    visible in the run summary (the AttemptClosed payload has no detail field).
    ``role`` names the role whose run failed (usage-row honesty only — the
    core contract is identical).
    """
    current = rt.attempts.get(aid)
    if isinstance(current, Mapping) and current.get("closed") is True:
        # External cancel already closed the attempt; do not reopen or retry.
        return
    await rt.fail_attempt(aid, retryable=True, close_reason=CloseReason.FAILED)
    usages.append(
        {
            "role": role,
            "task_key": task_key,
            "attempt_id": aid,
            "retryable_error": detail,
        }
    )


async def _fail_stage_attempt_retryable(
    rt: StudyRuntime,
    aid: str,
    *,
    role: str,
    task_key: str,
    detail: str,
    usages: list[dict[str, object]],
) -> bool:
    """Close one stage-lane attempt as a retryable failure; report the re-arm.

    Same core contract as :func:`_fail_attempt_retryable` (AttemptClosed
    failed + retryable via ``rt.fail_attempt``), for the stage lanes the run
    driver itself re-drives in place (SPEC/HYP/LIT): a role-call LLM failure
    (transport timeout, …) is ATTEMPT-level, not run-fatal. The core owns the
    budget: ``charged < max_attempts`` re-arms the task (PENDING + retry_at)
    → returns True and the lane starts the fresh attempt; exhaustion writes
    TaskDone(FAILED) → returns False so the lane surfaces the honest typed
    refuse instead of re-starting a DONE task. The error detail stays visible
    in the run summary (the AttemptClosed payload has no detail field).
    """
    current = rt.attempts.get(aid)
    if isinstance(current, Mapping) and current.get("closed") is True:
        # External cancel already closed the attempt; do not reopen or retry.
        return False
    await rt.fail_attempt(aid, retryable=True, close_reason=CloseReason.FAILED)
    usages.append(
        {
            "role": role,
            "task_key": task_key,
            "attempt_id": aid,
            "retryable_error": detail,
        }
    )
    task = _task_by_key(rt, task_key)
    return task is not None and task.state == TaskState.PENDING


async def _execute_executor_attempt(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    draft: TaskSpecDraft,
    aid: str,
    planned: tuple[PlannedToolCall, ...] | None,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    allow_tools: Sequence[_NamedTool],
    role_budget: Budget,
    usages: list[dict[str, object]],
    ctx: StudyContext | None,
) -> None:
    """Run one task attempt and journal SUCCESS or a retryable failure.

    Route dispatch by ``draft.route``: ``hypotheses`` tasks run the wired
    hypothesizer role (stage-equivalent; see
    :func:`_execute_hypotheses_task_attempt`), every other route runs the
    executor-route lane below — unmapped routes keep today's fail-closed
    behavior (no silent hypothesizer fallback).

    Runtime-drift pre-checks (no executor / attempt row / task row) refuse
    terminal rc=4 BEFORE any adapter call — infra drift must not burn the
    retry budget or surface as an rc=7 science failure. Transient model/tool
    failures (``AgentError``/``ToolError``/``LlmError`` from the adapter,
    ``run.error``, model-output record-contract refusals) close the attempt
    retryable so the core re-arms it. Budget-exceeded ``AgentError`` also
    stays terminal (rc=4): the budget gate never retries.

    Retry facts (owner directive): on a core-fired retry (attempt N>1) the
    PREVIOUS attempt's typed failure reason rides the role input as a
    user-role message (``prior_failure``) — one seam for both routes here
    (executor + hypotheses), no extra LLM call, budget/limits untouched.
    """
    # Drift subset of record_executor_route refusals, hoisted before dispatch
    # (single-threaded runtime: the state cannot change between check and use).
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    attempt_row = rt.attempts.get(aid)
    if not isinstance(attempt_row, Mapping):
        raise CliRefuse(f"attempt {aid!r} missing from runtime.attempts", code=4)
    row_task_id = attempt_row.get("task_id")
    if type(row_task_id) is not str or not row_task_id or rt.tasks.get(row_task_id) is None:
        raise CliRefuse(f"attempt {aid!r} has no resolvable task (drift); refuse", code=4)
    prior_failure = _prior_attempt_failure_reason(rt, usages, draft.task_key, aid)
    # Route dispatch (design rule: «route определяет исполнителя» — the route
    # kind picks the executor, task shape and provider never do):
    # experiment/other → executable-tool executor lane below; coder delivery
    # stays the driver-level CODER_HINT dispatch; hypotheses → hypothesizer.
    if draft.route.strip().lower() == TaskKind.HYPOTHESES.value:
        if ctx is None:
            # Continue lane: the validated stage context is run-driver-built
            # and never journaled — no half-reconstructed hypothesizer input.
            raise CliRefuse(
                f"task {draft.task_key!r} route 'hypotheses' cannot be re-driven "
                "by continue (no journaled hypothesizer context); re-run from "
                "`coscientist run` with the same --study-id",
                code=3,
            )
        if not adapter.can_run_role(_ROLE_HYP):
            # Wiring gap, terminal before any attempt burns (rc=5 seam, same
            # contract as the wired HYP stage's unwired refuse).
            raise CliRefuse(
                f"task {draft.task_key!r} route 'hypotheses' needs the hypothesizer "
                "role wired (--wired-roles + profile hypothesizer role); refuse",
                code=5,
            )
        await _execute_hypotheses_task_attempt(
            rt, adapter=adapter, draft=draft, aid=aid, ctx=ctx, role_budget=role_budget, usages=usages,
            prior_failure=prior_failure,
        )
        return
    if isinstance(adapter, LlmAgentAdapter):
        adapter.bind_tool_executor(
            RuntimeMcpExecutor(rt, adapter.tool_router, frozenset(t.spec.name for t in allow_tools))
        )
    exec_study_request = ""
    if rt.study is not None:
        exec_study_request = rt.study.request
    try:
        exec_run = await adapter.run(
            "executor-route",
            (
                AutomaticExecutorIn(
                    mode="automatic",
                    request=exec_study_request,
                    task=draft,
                    study_id=study_id,
                    attempt_id=aid,
                    inputs=tuple(registered_inputs),
                )
                if planned is None
                else ExecutorRouteIn(
                    task=draft,
                    study_id=study_id,
                    attempt_id=aid,
                    tool_args=planned,
                    # Study-level inputs wired to every derived task — the same
                    # uniform binding core applies to attempts
                    # (study_user_input_bindings); derivation-side, not LLM-chosen.
                    inputs=tuple(registered_inputs),
                )
            ),
            list(allow_tools),
            role_budget,
            prior_failure=prior_failure,
        )
    except AgentError as exc:
        if exc.detail.startswith("budget."):
            raise CliRefuse(f"agent error: {exc.detail}", code=4) from exc
        await _fail_attempt_retryable(rt, aid, task_key=draft.task_key, detail=exc.detail, usages=usages)
        return  # audit-ok[except_silent_return]: attempt closed retryable via core fail_attempt above
    except (ToolError, LlmError) as exc:
        if isinstance(exc, ToolError):
            detail = f"tool error: {exc.detail}"
        else:
            detail = f"llm error: {exc}"
        await _fail_attempt_retryable(rt, aid, task_key=draft.task_key, detail=detail, usages=usages)
        return  # audit-ok[except_silent_return]: attempt closed retryable via core fail_attempt above
    usages.append(_usage_row("executor-route", exec_run, task_key=draft.task_key))
    if exec_run.error is not None:
        await _fail_attempt_retryable(
            rt,
            aid,
            task_key=draft.task_key,
            detail=f"executor-route error: {exec_run.error}",
            usages=usages,
        )
        return
    try:
        await record_executor_route(rt, aid, exec_run)
    except CliRefuse as exc:
        # Drift refusals were pre-checked above, so every refusal reaching
        # here is a MODEL-OUTPUT contract violation (values JSON, missing
        # expected output, missing tool result, wrong output type): the
        # attempt gets a fresh bounded retry rather than a whole-run refuse.
        await _fail_attempt_retryable(
            rt, aid, task_key=draft.task_key, detail=f"record refused: {exc.detail}", usages=usages
        )


async def _execute_hypotheses_task_attempt(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    draft: TaskSpecDraft,
    aid: str,
    ctx: StudyContext,
    role_budget: Budget,
    usages: list[dict[str, object]],
    prior_failure: str | None = None,
) -> None:
    """One hypotheses-route task attempt through the wired hypothesizer role.

    Stage-equivalent execution (the wired HYP lane in
    :func:`_run_wired_hyp_stage`): the SAME validated ``StudyContext`` in, the
    hypothesizer's schema-validated ``HypothesisOut`` out, recorded through
    the SAME :func:`record_hypotheses` path (hypothesis graph nodes +
    PI1b-gated artifacts; with no declared ``expected_outputs`` the record is
    values-only and the task still closes SUCCESS). Differences are lane
    mechanics only: retries are the CORE's (``_fail_attempt_retryable`` —
    the tick loop re-arms, no in-place stage re-drive) and no HITL stage gate
    (task attempts have none; the stage lane keeps its gate).

    Caller (``_execute_executor_attempt``) has already verified the adapter
    can run the role and hoisted the runtime-drift checks.
    """
    while True:
        try:
            hyp_run = await adapter.run(_ROLE_HYP, ctx, [], role_budget, prior_failure=prior_failure)
            break
        except AgentError as exc:
            if exc.detail.startswith("budget."):
                raise CliRefuse(f"agent error: {exc.detail}", code=4) from exc
            await _fail_attempt_retryable(
                rt, aid, task_key=draft.task_key, detail=exc.detail, usages=usages, role=_ROLE_HYP
            )
            return  # audit-ok[except_silent_return]: attempt closed retryable via core fail_attempt above
        except LlmError as exc:
            await _fail_attempt_retryable(
                rt, aid, task_key=draft.task_key, detail=f"llm error: {exc}", usages=usages, role=_ROLE_HYP
            )
            return  # audit-ok[except_silent_return]: attempt closed retryable via core fail_attempt above
    usages.append(_usage_row(_ROLE_HYP, hyp_run, task_key=draft.task_key))
    if hyp_run.error is not None:
        await _fail_attempt_retryable(
            rt,
            aid,
            task_key=draft.task_key,
            detail=f"hypothesizer error: {hyp_run.error}",
            usages=usages,
            role=_ROLE_HYP,
        )
        return
    if not isinstance(hyp_run.output, HypothesisOut):
        # Adapter contract (schema-validated output class) — a violation is a
        # model-output contract failure: retryable, like the executor lane's
        # record refusals. Never journal a non-validated payload.
        await _fail_attempt_retryable(
            rt,
            aid,
            task_key=draft.task_key,
            detail=f"hypothesizer must return HypothesisOut, got {type(hyp_run.output).__name__}",
            usages=usages,
            role=_ROLE_HYP,
        )
        return
    try:
        await record_hypotheses(rt, aid, hyp_run.output)
    except CliRefuse as exc:
        # Drift refusals were pre-checked above, so a refusal reaching here is
        # a record-contract violation: fresh bounded retry, not a run refuse.
        await _fail_attempt_retryable(
            rt, aid, task_key=draft.task_key, detail=f"record refused: {exc.detail}", usages=usages, role=_ROLE_HYP
        )


async def _drive_plan_attempts(
    rt: StudyRuntime,
    *,
    plan_out: PlanOut,
    tools: CliToolSurface,
    adapter: LlmAgentAdapter,
    study_id: str,
    registered_inputs: Sequence[RegisteredInput],
    role_budget: Budget,
    usages: list[dict[str, object]],
    ctx: StudyContext | None,
    allowed_tool_names: frozenset[str],
    executed_aids: set[str],
    coder_dispatched: set[str],
    coder_backend: CoderBackend | None,
    coder_jobs: Mapping[str, _CoderJobDecl] | None,
    writer_journal: _WriterJobJournal,
    wall: Clock,
    hitl_policy: HitlPolicy,
    deadline: datetime,
    remote_runtime: RemoteCoderRuntime | None = None,
    defer_report: bool = False,
) -> None:
    """One dispatch pass over the accepted plan (called once per loop tick).

    Per draft, in plan order:
    - coder delivery: dispatched at most once per task key (job lifecycle is
      the coder backend's own; failures refuse the run as before);
    - executor delivery: FIRST attempts are CLI-started for dispatchable
      tasks (no prior attempts); core-fired retry attempts (started by
      ``_fire_due_retries``) are detected as open+unexecuted and run exactly
      once. Tasks waiting on deps or retry_at are skipped this pass — the
      core's tick owns their scheduling (no parallel retry loop here).

    After the accepted-plan drafts, a second pass drives tasks that exist
    only in the journal fold — PLAN-n replan containers, tasks added by an
    applied PlanExpanded, foreach materializations. SAME delivery rules,
    SAME shared budget/deadline, no second runner: dispatch state is read
    from ``rt`` (never from ``plan_out``), so an expansion applied by the
    core mid-run is picked up on the next pass and executed like any other
    task. Expanded coder tasks demand an exact task-key job binding (no
    positional reuse of original jobs; a missing binding is a typed
    refusal, never a silent skip).

    ``defer_report`` (HITL v2 §5): the interactive report gate keeps the
    REPORT placeholder pending as the finalize barrier — this pass skips it
    (the loop's gate check owns closing it) instead of the typed refuse.
    """
    dispatchable = {t.spec.task_key for t in rt.iter_dispatchable_tasks()}
    allow_tools = list(tools.named_tools())
    coder_bind = _bind_coder_jobs(plan_out, coder_jobs) if coder_jobs else {}
    for draft in plan_out.tasks:
        task = _task_by_key(rt, draft.task_key)
        if task is None:
            raise CliRefuse(
                f"plan task {draft.task_key!r} missing from runtime tasks (plan/journal drift); refuse",
                code=3,
            )
        if task.state == TaskState.DONE:
            continue
        if CODER_HINT in draft.tools_hint:
            if draft.task_key in coder_dispatched:
                continue
            if task.attempts or draft.task_key not in dispatchable:
                # Not yet startable this pass (deps pending) — the tick loop
                # re-offers it next iteration.
                continue
            coder_dispatched.add(draft.task_key)
            # Code-execution delivery: dispatch the declared coder job through
            # the coder backend (JobStore lifecycle + job artifacts). Typed
            # refuse without explicit --coder-backend (no stub science).
            if coder_backend is None:
                raise CliRefuse(
                    f"task {draft.task_key!r} needs code execution "
                    f"(tools_hint {CODER_HINT!r}) but --coder-backend is not set; "
                    f"refuse (no stub science; explicit bwrap|local-python|remote-openhands required)",
                    code=5,
                )
            if resolve_coder_backend(coder_backend) == "remote-openhands":
                # Model-authored task content on the legacy OpenHands service
                # (slice-2): no operator coder_jobs is consulted or required.
                if remote_runtime is None:
                    raise CliRefuse(
                        f"task {draft.task_key!r} needs the remote coder backend but "
                        f"no remote coder runtime is composed (--coder-remote-url "
                        f"required); refuse",
                        code=4,
                    )
                await _dispatch_remote_coder_task(
                    rt,
                    draft=draft,
                    study_id=study_id,
                    adapter=adapter,
                    runtime=remote_runtime,
                    writer_journal=writer_journal,
                    clock=wall,
                    hitl_policy=hitl_policy,
                    deadline=deadline,
                    usages=usages,
                    role_budget=role_budget,
                    registered_inputs=registered_inputs,
                )
                continue
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
                decl=coder_bind[draft.task_key],
                writer_journal=writer_journal,
                clock=wall,
                hitl_policy=hitl_policy,
                deadline=deadline,
                usages=usages,
            )
            continue
        open_aids = [aid for aid in task.attempts if attempt_row_is_open(rt.attempts, aid)]
        fresh = [aid for aid in open_aids if aid not in executed_aids]
        if fresh:
            # Core-fired retry (or first attempt after start below): execute.
            planned = (
                None
                if isinstance(tools, LiveCliToolSurface) and tools.automatic
                else _resolve_planned_calls(tools, draft)
            )
            for aid in fresh:
                executed_aids.add(aid)
                await _execute_executor_attempt(
                    rt,
                    adapter=adapter,
                    draft=draft,
                    aid=aid,
                    planned=planned,
                    study_id=study_id,
                    registered_inputs=registered_inputs,
                    allow_tools=allow_tools,
                    role_budget=role_budget,
                    usages=usages,
                    ctx=ctx,
                )
            continue
        if not open_aids and not task.attempts and draft.task_key in dispatchable:
            # First attempt: CLI starts it (retries are always core-started).
            planned = (
                None
                if isinstance(tools, LiveCliToolSurface) and tools.automatic
                else _resolve_planned_calls(tools, draft)
            )
            aid = await _require_attempt(rt, draft.task_key)
            executed_aids.add(aid)
            await _execute_executor_attempt(
                rt,
                adapter=adapter,
                draft=draft,
                aid=aid,
                planned=planned,
                study_id=study_id,
                registered_inputs=registered_inputs,
                allow_tools=allow_tools,
                role_budget=role_budget,
                usages=usages,
                ctx=ctx,
            )

    # Adaptive pass — journal-fold tasks absent from the accepted plan
    # (PLAN-n replan containers, PlanExpanded additions, foreach children).
    # Journal order is deterministic, so live execution and replay offer the
    # same dispatch sequence. Stage placeholders (SPEC/PLAN/HYP/LIT/REPORT)
    # never legitimately reach this loop pending; if one does, refuse typed
    # instead of hanging the tick loop.
    from coscientist.cli.continue_cmd import draft_from_task_spec, spec_redrivable

    initial_keys = {d.task_key for d in plan_out.tasks}
    for task in list(rt.tasks.values()):
        tkey = task.spec.task_key
        if tkey in initial_keys or task.state == TaskState.DONE:
            # Initial drafts ran in pass 1; DONE tasks (including stage
            # containers closed by accept_plan, whose attempt rows may stay
            # open) are never re-driven.
            continue
        if CODER_HINT in task.spec.tools_hint:
            # Coder delivery is dispatch-once per key (job lifecycle is the
            # backend's own). On remote-openhands the content is model-
            # authored and no operator coder_jobs exists (pass-1 parity), so
            # an expanded task dispatches without a binding; on the other
            # backends an exact task-key job binding is required — never a
            # positional reuse of an original job (a missing binding is a
            # typed refusal, never a silent skip).
            if tkey in coder_dispatched or task.attempts or tkey not in dispatchable:
                continue
            if coder_backend is None:
                raise CliRefuse(
                    f"task {tkey!r} needs code execution (tools_hint "
                    f"{CODER_HINT!r}) but --coder-backend is not set; refuse",
                    code=5,
                )
            if resolve_coder_backend(coder_backend) == "remote-openhands":
                if remote_runtime is None:
                    raise CliRefuse(
                        f"task {tkey!r} added after accept_plan needs the remote "
                        f"coder backend but no remote coder runtime is composed "
                        f"(--coder-remote-url required); refuse",
                        code=4,
                    )
                coder_dispatched.add(tkey)
                await _dispatch_remote_coder_task(
                    rt,
                    draft=draft_from_task_spec(task.spec),
                    study_id=study_id,
                    adapter=adapter,
                    runtime=remote_runtime,
                    writer_journal=writer_journal,
                    clock=wall,
                    hitl_policy=hitl_policy,
                    deadline=deadline,
                    usages=usages,
                    role_budget=role_budget,
                    registered_inputs=registered_inputs,
                )
                continue
            decl = coder_bind.get(tkey)
            if decl is None:
                raise CliRefuse(
                    f"task {tkey!r} added after accept_plan needs code execution "
                    f"but no --coder-jobs binding declares its task key "
                    f"(bound={sorted(coder_bind)!r}); refuse (no positional job reuse)",
                    code=3,
                )
            coder_dispatched.add(tkey)
            await _dispatch_coder_task(
                rt,
                draft=draft_from_task_spec(task.spec),
                study_id=study_id,
                backend=coder_backend,
                decl=decl,
                writer_journal=writer_journal,
                clock=wall,
                hitl_policy=hitl_policy,
                deadline=deadline,
                usages=usages,
            )
            continue
        open_aids = [aid for aid in task.attempts if attempt_row_is_open(rt.attempts, aid)]
        fresh = [aid for aid in open_aids if aid not in executed_aids]
        first_start = not open_aids and not task.attempts and tkey in dispatchable
        if not fresh and not first_start:
            # Deps pending, retry backoff, or already driven — the core's
            # tick owns scheduling; no parallel loop here.
            continue
        if tkey in _STAGE_TASK_KEYS:
            if defer_report and tkey == "REPORT":
                # HITL v2 report-gate barrier: REPORT stays pending until the
                # gate flow closes it (the loop's gate check fires when it is
                # the last pending task); the driver must not refuse on it.
                continue
            raise CliRefuse(
                f"stage task {tkey!r} is pending in the run loop; stage roles are "
                f"driven by their own lanes — refuse (no silent re-run)",
                code=3,
            )
        if not spec_redrivable(task.spec):
            raise CliRefuse(
                f"task {tkey!r} added after accept_plan carries core-owned spec "
                f"fields the executor route cannot carry; refuse (no silent "
                f"spec rewrite)",
                code=3,
            )
        draft = draft_from_task_spec(task.spec)
        planned = (
            None if isinstance(tools, LiveCliToolSurface) and tools.automatic else _resolve_planned_calls(tools, draft)
        )
        if first_start:
            aid = await _require_attempt(rt, tkey)
            executed_aids.add(aid)
            await _execute_executor_attempt(
                rt,
                adapter=adapter,
                draft=draft,
                aid=aid,
                planned=planned,
                study_id=study_id,
                registered_inputs=registered_inputs,
                allow_tools=allow_tools,
                role_budget=role_budget,
                usages=usages,
                ctx=ctx,
            )
            continue
        for aid in fresh:
            executed_aids.add(aid)
            await _execute_executor_attempt(
                rt,
                adapter=adapter,
                draft=draft,
                aid=aid,
                planned=planned,
                study_id=study_id,
                registered_inputs=registered_inputs,
                allow_tools=allow_tools,
                role_budget=role_budget,
                usages=usages,
                ctx=ctx,
            )




__all__ = ["main", "run_study"]
