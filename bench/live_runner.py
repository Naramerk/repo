"""New-system adapter (plan item 13): the live CoScientist CLI as a BenchRunner.

Plan deviation (documented): item 13 names the agent-layer ``Orchestrator``
(G1/G1b, #66) as the entrypoint — no such composition object exists; the
sanctioned live path is the CLI ``coscientist.cli.run`` (the API lane reuses the
exact same function, so this adapter is single-source with it). Every
``BenchTask`` is mapped mechanically onto the ``coscientist run`` flag surface
and executed as a SUBPROCESS (isolation from the harness process; a hung study
is bounded by the required ``timeout_s``). The run's final summary JSON line is
the declared observed outcome: verdict/phase/usage plus the additive
``quality`` block (see ``coscientist.cli.run._observed_quality``).

Honest accounting rules:

- Usage rows: only rows carrying the LLM token keys (``prompt_tokens`` and
  ``completion_tokens``) are LLM calls (``_usage_row`` shape, lane rows
  included). Rows without them (coder job rows, retry notes, critic-issue
  notes) are not LLM calls and are not counted; tokens are never estimated.
- Verdict mapping: exit code 0 (fulfilled) and 7 (closed with a non-fulfilled
  verdict) are OBSERVED outcomes -> ``RunnerSuccess`` with the observed quality;
  the exact compare against task data happens in ``bench.quality``. Every other
  exit code is a refusal before/around closure -> ``RunnerRefuse``.
- Driver tasks (``driver: web|recovery``) are typed-refused: the webdriver lane
  is not part of the CLI path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from bench.errors import BenchConfigError
from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    UsageRecorded,
)
from coscientist.llm.types import Usage

# Summary usage rows carrying both token keys are the LLM-call rows (the
# ``_usage_row`` shape; report/review lanes emit the same token keys).
_USAGE_TOKEN_KEYS = ("prompt_tokens", "completion_tokens")


class LiveRunnerConfig(BaseModel):
    """Explicit CLI wiring for the live runner — every field required.

    No defaults are invented anywhere: nullable fields (``tools_json`` /
    ``mcp_tool_bundle`` / ``mcp_tool_call_args`` / ``coder_backend`` /
    ``coder_jobs``) must still be written explicitly in the runner config YAML,
    and the tool path is validated as cassette XOR live MCP (mirroring
    ``coscientist.cli.run._validate_tool_cli_flags``). ``task_inputs_dir`` is
    the directory ``BenchTask.inputs`` paths resolve against (the task-set
    directory of the suite being run); ``study_id_prefix`` names the run so
    each task's study id is ``<prefix>-<task_id>``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # LLM + catalog wiring (exact pins, no overrides).
    llm_config: Path
    catalog: Path
    catalog_version: int
    profile: str
    tool_catalog_version: int
    planner_model: str
    executor_model: str
    tz_spec_model: str
    owner: str
    # Tool path: cassette (--tools-json) XOR live MCP (--mcp-tool-bundle plus
    # its required --tool-call-args).
    tools_json: Path | None
    mcp_tool_bundle: Path | None
    mcp_tool_call_args: Path | None
    # Coder path (absent -> code-execution tasks typed-refuse inside the CLI).
    coder_backend: Literal["bwrap", "local-python"] | None
    coder_jobs: Path | None
    # Task inputs + journals.
    task_inputs_dir: Path
    data_root: Path
    study_id_prefix: str
    # Study quotas / budgets (required CLI flags, no defaults).
    deadline_s: float
    require_hypotheses: bool
    confirmations_needed: int
    refutations_needed: int
    refutation_priority: bool
    supported_needed: int
    max_expansions: int
    finalize_reserve_s: int
    lease_ttl_s: float
    rng_seed: int
    max_llm_calls: int
    max_tokens: int
    max_tool_calls: int
    max_ticks: int
    hitl_mode: Literal["interactive", "auto", "headless"]
    wired_roles: tuple[str, ...]
    # Subprocess isolation.
    subprocess_cwd: Path
    timeout_s: float

    @model_validator(mode="after")
    def _tool_path_xor(self) -> LiveRunnerConfig:
        cassette = self.tools_json is not None
        mcp = self.mcp_tool_bundle is not None
        call_args = self.mcp_tool_call_args is not None
        if cassette and mcp:
            raise ValueError("tools_json and mcp_tool_bundle are mutually exclusive (cassette XOR live MCP)")
        if not cassette and not mcp:
            raise ValueError("one of tools_json or mcp_tool_bundle is required")
        if mcp and not call_args:
            raise ValueError("mcp_tool_call_args is required with mcp_tool_bundle")
        if call_args and not mcp:
            raise ValueError("mcp_tool_call_args is only valid with mcp_tool_bundle")
        if self.coder_jobs is not None and self.coder_backend is None:
            raise ValueError("coder_backend is required with coder_jobs")
        return self

    @classmethod
    def from_yaml_file(cls, path: Path) -> LiveRunnerConfig:
        """Load a runner config YAML; every wiring problem is a typed error."""
        if not path.is_file():
            raise BenchConfigError(f"runner config missing: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise BenchConfigError(f"runner config is not valid YAML: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise BenchConfigError(f"runner config must be a YAML mapping: {path}")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise BenchConfigError(f"runner config invalid: {path}: {exc}") from exc


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


def _stderr_tail(stderr: str) -> str:
    """Last non-empty stderr line (CLI refusals print one 'cli refuse: ...' line)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "no stderr output"


def _parse_summary(stdout: str) -> dict[str, Any]:
    """Final non-empty stdout line must be the run-summary JSON object."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("stdout has no summary line")
    loaded = json.loads(lines[-1])
    if not isinstance(loaded, dict):
        raise ValueError("summary line is not a JSON object")
    return loaded


def _usage_rows_to_recorded(summary: dict[str, Any], study_id: str) -> tuple[UsageRecorded, ...]:
    """Map LLM token rows of the summary onto ``UsageRecorded`` payloads.

    Rows without both token keys are not LLM calls (coder job rows, retry and
    critic-issue notes) and are skipped — the skip is the accounting rule above,
    never a silent drop of tokens. ``task_id`` is the study-graph task key when
    the row declares one; ``attempt_id`` is not part of the summary rows.
    """
    records: list[UsageRecorded] = []
    for row in summary["usage"]:
        if not all(key in row for key in _USAGE_TOKEN_KEYS):
            continue
        records.append(
            UsageRecorded(
                study_id=study_id,
                task_id=row["task_key"] if "task_key" in row else None,
                attempt_id=None,
                role=row["role"],
                usage=Usage(
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    latency_ms=row["latency_ms"] if "latency_ms" in row else 0.0,
                ),
            )
        )
    return tuple(records)


def _evidence_rows(summary: dict[str, Any]) -> tuple[EvidenceOutcomeCount, ...]:
    return tuple(
        EvidenceOutcomeCount(outcome=row["outcome"], count=row["count"]) for row in summary["quality"]["evidence"]
    )


def _success_from_summary(task: BenchTask, summary: dict[str, Any]) -> RunnerSuccess:
    """Observed-quality ``RunnerSuccess`` parsed from the run-summary line."""
    study_id = str(summary["study_id"])
    verdict = summary["verdict"]
    if verdict is None:
        raise ValueError("summary verdict is null (study closed without a verdict)")
    return RunnerSuccess(
        kind="success",
        usage_recorded=_usage_rows_to_recorded(summary, study_id),
        quality=QualityObservation(
            status=str(verdict),
            exp_tasks=summary["quality"]["exp_tasks"],
            evidence=_evidence_rows(summary),
            record_result=summary["quality"]["record_result"],
        ),
    )


class LiveRunner:
    """BenchRunner executing each task through the real CLI subprocess."""

    def __init__(self, config: LiveRunnerConfig) -> None:
        self.config = config

    def run(self, task: BenchTask) -> RunnerSuccess | RunnerRefuse:
        if task.driver is not None:
            return RunnerRefuse(
                kind="refused",
                reason_code="driver_task",
                detail=(
                    f"driver tasks not supported by live adapter yet "
                    f"(task {task.task_id!r} declares driver {task.driver!r})"
                ),
            )
        study_id = f"{self.config.study_id_prefix}-{task.task_id}"
        journal_dir = self.config.data_root / study_id
        if journal_dir.exists():
            return RunnerRefuse(
                kind="refused",
                reason_code="study_journal_exists",
                detail=f"refusing to overwrite an existing study journal: {journal_dir}",
            )
        input_paths: list[Path] = []
        for input_rel in task.inputs:
            input_path = self.config.task_inputs_dir / input_rel
            if not input_path.is_file():
                return RunnerRefuse(
                    kind="refused",
                    reason_code="task_input_missing",
                    detail=f"task {task.task_id!r} input not found under task_inputs_dir: {input_path}",
                )
            input_paths.append(input_path)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as task_file:
            task_file.write(task.prompt)
            task_path = Path(task_file.name)
        try:
            argv = build_run_argv(
                self.config,
                task_file=task_path,
                study_id=study_id,
                input_paths=tuple(input_paths),
            )
            start = time.perf_counter()
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    cwd=self.config.subprocess_cwd,
                    timeout=self.config.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return RunnerRefuse(
                    kind="refused",
                    reason_code="timeout",
                    detail=(
                        f"coscientist run exceeded timeout_s={self.config.timeout_s} "
                        f"after {time.perf_counter() - start:.1f}s (study {study_id!r})"
                    ),
                )
            return _result_from_process(
                task,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                study_id=study_id,
            )
        finally:
            task_path.unlink()


def _result_from_process(
    task: BenchTask,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    study_id: str,
) -> RunnerSuccess | RunnerRefuse:
    """Typed result from the finished subprocess (exit-code contract of run.py).

    Exit 0 (fulfilled) and 7 (closed with a non-fulfilled verdict) both wrote a
    summary line -> observed outcome. Any other exit code refused before/around
    closure (bad wiring 2, plan refusal 3, agent failure 4, missing wiring 5,
    max_ticks 6, lane failure 8) -> ``RunnerRefuse``.
    """
    if returncode not in (0, 7):
        detail = _stderr_tail(stderr)
        return RunnerRefuse(
            kind="refused",
            reason_code=f"exit_{returncode}",
            detail=f"coscientist run exited {returncode} (study {study_id!r}): {detail}",
        )
    try:
        summary = _parse_summary(stdout)
        return _success_from_summary(task, summary)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        return RunnerRefuse(
            kind="refused",
            reason_code="summary_unreadable",
            detail=(
                f"could not parse the run summary line for study {study_id!r} "
                f"(exit {returncode}): {type(exc).__name__}: {exc}"
            ),
        )
