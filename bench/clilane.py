"""CLI-lane mechanical mapping (plan item 13): ``coscientist run`` argv + result.

Shared by the plain CLI lane (``bench.live_runner``) and the recovery driver
(``bench.drivers.recovery`` starts the same run argv before the SIGKILL), so
the flag-surface mapping and the exit-code contract stay single-source. The
exit-code contract (``result_from_process``): exit 0 (fulfilled) and 7 (closed
with a non-fulfilled verdict) both wrote a summary line -> observed outcome
(``RunnerSuccess``); every other exit code refused before/around closure
(bad wiring 2, plan refusal 3, agent failure 4, missing wiring 5, max_ticks 6,
lane failure 8) -> ``RunnerRefuse``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from bench.models import RunnerRefuse, RunnerSuccess
from bench.runsummary import parse_summary, stderr_tail, success_from_summary

if TYPE_CHECKING:
    from bench.live_runner import LiveRunnerConfig
    from bench.models import BenchTask


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def build_run_argv(
    config: LiveRunnerConfig, *, task_file: Path, study_id: str, input_paths: tuple[Path, ...]
) -> list[str]:
    """Mechanical ``coscientist run`` argv from the config + task mapping."""
    argv = [
        sys.executable,
        "-m",
        "coscientist.cli",
        "run",
        str(task_file),
        "--data-root",
        str(config.data_root),
        "--deadline-s",
        str(config.deadline_s),
        "--catalog-version",
        str(config.catalog_version),
        "--catalog",
        str(config.catalog),
        "--profile",
        config.profile,
        "--tool-catalog-version",
        str(config.tool_catalog_version),
        "--owner",
        config.owner,
        "--llm-config",
        str(config.llm_config),
        "--planner-model",
        config.planner_model,
        "--executor-model",
        config.executor_model,
        "--tz-spec-model",
        config.tz_spec_model,
        "--study-id",
        study_id,
        "--require-hypotheses",
        _format_bool(config.require_hypotheses),
        "--confirmations-needed",
        str(config.confirmations_needed),
        "--refutations-needed",
        str(config.refutations_needed),
        "--refutation-priority",
        _format_bool(config.refutation_priority),
        "--supported-needed",
        str(config.supported_needed),
        "--max-expansions",
        str(config.max_expansions),
        "--finalize-reserve-s",
        str(config.finalize_reserve_s),
        "--lease-ttl-s",
        str(config.lease_ttl_s),
        "--rng-seed",
        str(config.rng_seed),
        "--max-llm-calls",
        str(config.max_llm_calls),
        "--max-tokens",
        str(config.max_tokens),
        "--max-tool-calls",
        str(config.max_tool_calls),
        "--max-ticks",
        str(config.max_ticks),
        "--hitl-mode",
        config.hitl_mode,
        "--wired-roles",
        json.dumps(list(config.wired_roles)),
    ]
    if config.tools_json is not None:
        argv.extend(["--tools-json", str(config.tools_json)])
    else:
        # XOR validator guarantees bundle + call args are both set here.
        argv.extend(["--mcp-tool-bundle", str(config.mcp_tool_bundle)])
        argv.extend(["--tool-call-args", str(config.mcp_tool_call_args)])
    if config.coder_backend is not None:
        argv.extend(["--coder-backend", config.coder_backend])
    if config.coder_jobs is not None:
        argv.extend(["--coder-jobs", str(config.coder_jobs)])
    for input_path in input_paths:
        argv.extend(["--task-input", str(input_path)])
    return argv


def result_from_process(
    task: BenchTask,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    study_id: str,
) -> RunnerSuccess | RunnerRefuse:
    """Typed result from the finished run subprocess (the exit-code contract)."""
    if returncode not in (0, 7):
        detail = stderr_tail(stderr)
        return RunnerRefuse(
            kind="refused",
            reason_code=f"exit_{returncode}",
            detail=f"coscientist run exited {returncode} (study {study_id!r}): {detail}",
        )
    try:
        summary: dict[str, Any] = parse_summary(stdout)
        return success_from_summary(task, summary)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        return RunnerRefuse(
            kind="refused",
            reason_code="summary_unreadable",
            detail=(
                f"could not parse the run summary line for study {study_id!r} "
                f"(exit {returncode}): {type(exc).__name__}: {exc}"
            ),
        )
