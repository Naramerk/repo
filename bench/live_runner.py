"""New-system adapter (plan item 13): the live CoScientist CLI as a BenchRunner.

Plan deviation (documented): item 13 names the agent-layer ``Orchestrator``
(G1/G1b, #66) as the entrypoint — no such composition object exists; the
sanctioned live path is the CLI ``coscientist.cli.run`` (the API lane reuses the
exact same function, so this adapter is single-source with it). Every
``BenchTask`` is mapped mechanically onto the ``coscientist run`` flag surface
(``bench.clilane.build_run_argv``) and executed as a SUBPROCESS (isolation from
the harness process; a hung study is bounded by the required ``timeout_s``).
The run's final summary JSON line is the declared observed outcome:
verdict/phase/usage plus the additive ``quality`` block (see
``coscientist.cli.study_helpers._observed_quality``).

Driver lanes (plan gap 3): ``BenchTask.driver`` (``web`` | ``recovery`` | None
— task data, never a config field) selects the lane. After the SHARED
preflight (journal-exists guard, task-input resolution) the runner branches:
``None`` -> the CLI subprocess path, ``web`` -> ``bench.drivers.WebDriver``
(uvicorn API subprocess + POST /run + WS stream), ``recovery`` ->
``bench.drivers.RecoveryDriver`` (run -> journal marker -> SIGKILL ->
``coscientist continue``). Driver wiring is explicit config (``web_*`` /
``recovery_*`` / ``driver_out_dir`` fields, every one required — no defaults);
the lanes reuse the CLI flag mapping and the exit-code contract from
``bench.clilane``, and the summary parse/mapping from ``bench.runsummary``.

Honest accounting rules:

- Usage rows: only rows carrying the LLM token keys (``prompt_tokens`` and
  ``completion_tokens``) are LLM calls (``_usage_row`` shape, lane rows
  included). Rows without them (coder job rows, retry notes, critic-issue
  notes) are not LLM calls and are not counted; tokens are never estimated.
- Verdict mapping: exit code 0 (fulfilled) and 7 (closed with a non-fulfilled
  verdict) are OBSERVED outcomes -> ``RunnerSuccess`` with the observed quality;
  the exact compare against task data happens in ``bench.quality``. Every other
  exit code is a refusal before/around closure -> ``RunnerRefuse``.
- Driver lanes observe the same rule (see ``bench.drivers``): a run that dies
  mid-way is a ``RunnerSuccess`` with the observed status (``timeout`` or the
  closed verdict — HER-comparable outcomes); infra/wiring crashes refuse.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from bench.clilane import build_run_argv, result_from_process
from bench.drivers import Driver, RecoveryDriver, WebDriver
from bench.errors import BenchConfigError
from bench.models import BenchTask, RunnerRefuse, RunnerSuccess

_DRIVERS: dict[str, Driver] = {"web": WebDriver(), "recovery": RecoveryDriver()}


class LiveRunnerConfig(BaseModel):
    """Explicit CLI + driver wiring for the live runner — every field required.

    No defaults are invented anywhere: nullable fields (``tools_json`` /
    ``mcp_tool_bundle`` / ``mcp_tool_call_args`` / ``coder_backend`` /
    ``coder_jobs``) must still be written explicitly in the runner config YAML,
    and the tool path is validated as cassette XOR live MCP (mirroring
    ``coscientist.cli.run._validate_tool_cli_flags``). ``task_inputs_dir`` is
    the directory ``BenchTask.inputs`` paths resolve against (the task-set
    directory of the suite being run); ``study_id_prefix`` names the run so
    each task's study id is ``<prefix>-<task_id>``.

    Driver fields (used only when a task declares ``driver: web|recovery``,
    still required in every config — the wiring is declared up front, never
    inferred): ``driver_out_dir`` receives the lanes' event logs / summaries;
    ``web_*`` mirror the ``coscientist.api.serve`` flags plus the lane's
    transport bounds; ``recovery_*`` pin the kill marker (journal event type +
    ``entity_key`` substring), the marker-wait bound and the continue
    lease-wait (``--lease-wait-s``).
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
    # Driver lanes (bench.drivers; explicit wiring, no defaults).
    driver_out_dir: Path
    web_host: str
    web_port: int
    web_stream_poll_interval_s: float
    web_run_wait_s: float
    web_post_timeout_s: float
    web_stream_timeout_s: float
    web_connect_retries: int
    recovery_kill_event_type: str
    recovery_kill_entity_contains: str
    recovery_kill_wait_s: float
    recovery_lease_wait_s: float

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

    @model_validator(mode="after")
    def _driver_bounds(self) -> LiveRunnerConfig:
        if not 1 <= self.web_port <= 65535:
            raise ValueError(f"web_port must be 1..65535, got {self.web_port}")
        if self.web_stream_poll_interval_s <= 0.0:
            raise ValueError("web_stream_poll_interval_s must be positive")
        if self.web_run_wait_s <= 0.0:
            raise ValueError("web_run_wait_s must be positive")
        # The POST returns only once the run is journal-visible (bounded by the
        # server's --run-wait-s); a POST timeout at or below it would abort the
        # start wait before the server's own bound fires.
        if self.web_post_timeout_s <= self.web_run_wait_s:
            raise ValueError("web_post_timeout_s must be strictly above web_run_wait_s")
        if self.web_stream_timeout_s <= 0.0:
            raise ValueError("web_stream_timeout_s must be positive")
        if self.web_connect_retries < 0:
            raise ValueError("web_connect_retries must be >= 0")
        if not self.recovery_kill_event_type:
            raise ValueError("recovery_kill_event_type must be a non-empty journal event type")
        if not self.recovery_kill_entity_contains:
            raise ValueError("recovery_kill_entity_contains must be a non-empty entity_key substring")
        if self.recovery_kill_wait_s <= 0.0:
            raise ValueError("recovery_kill_wait_s must be positive")
        if self.recovery_lease_wait_s < 0.0:
            raise ValueError("recovery_lease_wait_s must be >= 0")
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


class LiveRunner:
    """BenchRunner executing each task through the real CLI subprocess."""

    def __init__(self, config: LiveRunnerConfig) -> None:
        self.config = config

    def run(self, task: BenchTask) -> RunnerSuccess | RunnerRefuse:
        preflight = _preflight(task, self.config)
        if isinstance(preflight, RunnerRefuse):
            return preflight
        study_id, input_paths = preflight
        if task.driver is None:
            return self._run_cli(study_id, input_paths, task)
        return _DRIVERS[task.driver].run(task, study_id=study_id, input_paths=input_paths, config=self.config)

    def _run_cli(self, study_id: str, input_paths: tuple[Path, ...], task: BenchTask) -> RunnerSuccess | RunnerRefuse:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as task_file:
            task_file.write(task.prompt)
            task_path = Path(task_file.name)
        try:
            argv = build_run_argv(
                self.config,
                task_file=task_path,
                study_id=study_id,
                input_paths=input_paths,
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
            return result_from_process(
                task,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                study_id=study_id,
            )
        finally:
            task_path.unlink()


def _preflight(task: BenchTask, config: LiveRunnerConfig) -> tuple[str, tuple[Path, ...]] | RunnerRefuse:
    """Shared lane preflight: study id, journal guard, task-input resolution.

    Every lane (CLI, web, recovery) refuses the same data problems the same
    way before any subprocess starts.
    """
    study_id = f"{config.study_id_prefix}-{task.task_id}"
    journal_dir = config.data_root / study_id
    if journal_dir.exists():
        return RunnerRefuse(
            kind="refused",
            reason_code="study_journal_exists",
            detail=f"refusing to overwrite an existing study journal: {journal_dir}",
        )
    input_paths: list[Path] = []
    for input_rel in task.inputs:
        input_path = config.task_inputs_dir / input_rel
        if not input_path.is_file():
            return RunnerRefuse(
                kind="refused",
                reason_code="task_input_missing",
                detail=f"task {task.task_id!r} input not found under task_inputs_dir: {input_path}",
            )
        input_paths.append(input_path)
    return study_id, tuple(input_paths)
