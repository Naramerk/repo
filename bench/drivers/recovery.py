"""Recovery-lane driver: run -> journal marker -> SIGKILL -> continue (PR #221).

The new-lane equivalent of the legacy ``recovery`` / ``recovery2`` benchmark
mode (old ``webdrive.py`` mode=recovery: stop_chat after the first
``record_result`` -> checkpoint restore -> resume). Mapping onto the sanctioned
recovery path (``bench/lab/driver/recovery_drive.sh`` contract):

1. ``coscientist run`` subprocess (the SHARED ``build_run_argv`` mapping — the
   exact CLI flag surface, no second construction);
2. poll the study journal read-only for the configured kill marker
   (``recovery_kill_event_type`` + ``recovery_kill_entity_contains`` — the lab
   driver's "first EXP ``ResultRecorded``") within ``recovery_kill_wait_s``;
3. SIGKILL the run's process group mid-study (a checkpoint-equals journal has
   no snapshot to take — the journal IS the checkpoint);
4. ``coscientist continue`` subprocess (``build_continue_argv`` — the CLI
   continue flag set verbatim from the shared config, lease takeover waits out
   the killed run's lease via ``recovery_lease_wait_s``);
5. verify: the continue summary verdict must equal the journal's
   ``StudyFinalized`` verdict (closure consistency; the deep restore
   invariants stay with the checker, ``bench/lab/driver/recovery_check.py``).

Honest outcome mapping (no verdict invention):

- continue exit 0 / 7 + verified closure -> ``RunnerSuccess``: verdict as
  status, quality from the shared ``observe_quality`` fold, usage rows from
  the continue summary (same ``_usage_row`` shape as run — the shared
  ``usage_rows_to_recorded`` mapping);
- the kill marker never appeared, or continue exceeded ``timeout_s``
  -> ``RunnerSuccess`` with status ``"timeout"`` and the partial journal
  observation (HER-comparable observed outcome; usage unobservable -> ``()``);
- the run process exits before the marker -> the same exit-code mapping as
  the CLI lane (``result_from_process`` on its captured stdout);
- continue CLI refusals (exit not in {0, 7}), a missing journal at verify,
  a verdict mismatch, or spawn failure -> typed ``RunnerRefuse``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bench.clilane import build_run_argv, result_from_process
from bench.drivers.journal import count_marker_events, observe_quality, read_journal_events
from bench.drivers.web import evidence_from
from bench.models import QualityObservation, RunnerRefuse, RunnerSuccess
from bench.runsummary import parse_summary, stderr_tail, usage_rows_to_recorded

if TYPE_CHECKING:
    from bench.live_runner import LiveRunnerConfig
    from bench.models import BenchTask

# Journal poll cadence while waiting for the kill marker (transport plumbing,
# mirrors the lab recovery_drive.sh poll loop, not benchmark semantics).
_MARKER_POLL_STEP_S = 0.2


def build_continue_argv(config: LiveRunnerConfig, *, data_root: Path, study_id: str) -> list[str]:
    """Mechanical ``coscientist continue`` argv from the shared config.

    Mirrors ``build_run_argv`` field-for-field on the continue flag surface
    (``coscientist.cli.run`` continue subcommand): no study-creation flags
    (deadline / thresholds / hitl are journaled and immutable — the CLI
    refuses drift), plus the recovery-specific ``--lease-wait-s``.
    """
    argv = [
        sys.executable,
        "-m",
        "coscientist.cli",
        "continue",
        str(data_root),
        "--study-id",
        study_id,
        "--catalog",
        str(config.catalog),
        "--catalog-version",
        str(config.catalog_version),
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
        "--lease-ttl-s",
        str(config.lease_ttl_s),
        "--lease-wait-s",
        str(config.recovery_lease_wait_s),
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
    return argv


class RecoveryDriver:
    """Kill-and-restore one study through the CLI run/continue subprocesses."""

    def run(
        self,
        task: BenchTask,
        *,
        study_id: str,
        input_paths: tuple[Path, ...],
        config: LiveRunnerConfig,
    ) -> RunnerSuccess | RunnerRefuse:
        out_dir = config.driver_out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as task_file:
            task_file.write(task.prompt)
            task_path = Path(task_file.name)
        try:
            return self._drive(
                task, study_id=study_id, input_paths=input_paths, config=config, task_path=task_path, out_dir=out_dir
            )
        finally:
            task_path.unlink()

    def _drive(
        self,
        task: BenchTask,
        *,
        study_id: str,
        input_paths: tuple[Path, ...],
        config: LiveRunnerConfig,
        task_path: Path,
        out_dir: Path,
    ) -> RunnerSuccess | RunnerRefuse:
        run_log = out_dir / f"{study_id}_run.log"
        db = config.data_root / study_id / "events.db"
        argv = build_run_argv(config, task_file=task_path, study_id=study_id, input_paths=input_paths)
        # File-backed logs (the lab recovery_drive.sh contract): a chatty
        # child can never stall on a full PIPE before the marker appears.
        with run_log.open("w", encoding="utf-8") as log_out:
            run_proc = subprocess.Popen(
                argv,
                cwd=str(config.subprocess_cwd),
                start_new_session=True,  # own process group: the SIGKILL lands on the whole group
                stdout=log_out,
                stderr=subprocess.STDOUT,
            )
            marker_seen: dict[str, Any] = {"seen": False, "waited_s": 0.0}
            kill_started = time.monotonic()
            deadline = kill_started + config.recovery_kill_wait_s
            while time.monotonic() < deadline:
                if run_proc.poll() is not None:
                    # The run ended before the kill marker: its own typed
                    # outcome is the honest answer (the scenario never armed).
                    return result_from_process(
                        task,
                        returncode=run_proc.returncode,
                        stdout=run_log.read_text(encoding="utf-8"),
                        stderr="",
                        study_id=study_id,
                    )
                if count_marker_events(
                    db,
                    study_id,
                    event_type=config.recovery_kill_event_type,
                    entity_contains=config.recovery_kill_entity_contains,
                ) >= 1:
                    marker_seen["seen"] = True
                    break
                time.sleep(_MARKER_POLL_STEP_S)
            marker_seen["waited_s"] = round(time.monotonic() - kill_started, 3)
            if not _kill_group(run_proc):
                return RunnerRefuse(
                    kind="refused",
                    reason_code="kill_group_unkillable",
                    detail="run process group did not terminate within 30s of SIGKILL",
                )
        if not marker_seen["seen"]:
            return observed_timeout(read_journal_events(db, study_id))
        return self._continue_phase(task, study_id=study_id, config=config, db=db, out_dir=out_dir, marker=marker_seen)

    def _continue_phase(
        self,
        task: BenchTask,
        *,
        study_id: str,
        config: LiveRunnerConfig,
        db: Path,
        out_dir: Path,
        marker: dict[str, Any],
    ) -> RunnerSuccess | RunnerRefuse:
        continue_log = out_dir / f"{study_id}_continue.log"
        argv = build_continue_argv(config, data_root=config.data_root, study_id=study_id)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=str(config.subprocess_cwd),
                timeout=config.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return observed_timeout(read_journal_events(db, study_id))
        continue_log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        if completed.returncode not in (0, 7):
            return RunnerRefuse(
                kind="refused",
                reason_code=f"exit_{completed.returncode}",
                detail=(
                    f"coscientist continue exited {completed.returncode} (study {study_id!r}): "
                    f"{stderr_tail(completed.stderr)}"
                ),
            )
        try:
            summary = parse_summary(completed.stdout)
            verdict = summary["verdict"]
            if verdict is None:
                raise ValueError("continue summary verdict is null")
            if not isinstance(summary["usage"], list):
                raise ValueError("continue summary usage is not a list")
            study = str(summary["study_id"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return RunnerRefuse(
                kind="refused",
                reason_code="summary_unreadable",
                detail=(
                    f"could not parse the continue summary line for study {study_id!r} "
                    f"(exit {completed.returncode}): {type(exc).__name__}: {exc}"
                ),
            )
        events = read_journal_events(db, study_id)
        observed = observe_quality(events)
        if observed["verdict"] != str(verdict) or observed["finalized"] != 1:
            return RunnerRefuse(
                kind="refused",
                reason_code="recovery_verify_failed",
                detail=(
                    f"continue verdict {verdict!r} does not match the journal closure "
                    f"(journal verdict={observed['verdict']!r}, "
                    f"StudyFinalized count={observed['finalized']}) for study {study_id!r}"
                ),
            )
        write_driver_summary(
            out_dir / f"{study_id}_summary.json",
            study_id,
            marker=marker,
            continue_exit=completed.returncode,
            verdict=str(verdict),
            observed=observed,
        )
        return RunnerSuccess(
            kind="success",
            usage_recorded=usage_rows_to_recorded(summary, study),
            quality=QualityObservation(
                status=str(verdict),
                exp_tasks=observed["exp_tasks"],
                evidence=evidence_from(observed),
                record_result=observed["record_result"],
            ),
        )


def observed_timeout(events: list[dict[str, Any]]) -> RunnerSuccess:
    """Observed mid-way death: status ``timeout``, partial journal fold, no usage."""
    observed = observe_quality(events)
    return RunnerSuccess(
        kind="success",
        usage_recorded=(),
        quality=QualityObservation(
            status="timeout",
            exp_tasks=observed["exp_tasks"],
            evidence=evidence_from(observed),
            record_result=observed["record_result"],
        ),
    )


def _kill_group(proc: subprocess.Popen[Any]) -> bool:
    """SIGKILL the run's process group; the killed lease is waited out by continue.

    Returns False when the group did not terminate within the wait budget —
    the caller refuses typed instead of proceeding against a live run.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return True


def write_driver_summary(
    path: Path,
    study_id: str,
    *,
    marker: dict[str, Any],
    continue_exit: int,
    verdict: str,
    observed: dict[str, Any],
) -> None:
    path.write_text(
        json.dumps(
            {
                "study_id": study_id,
                "kill_marker": marker,
                "continue_exit": continue_exit,
                "verdict": verdict,
                "observed": observed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
