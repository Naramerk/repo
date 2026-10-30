"""Web-lane driver: uvicorn API subprocess + POST /run + WS stream (plan gap 3).

Old-system contract (``webdrive.py`` mode=web over HTTP+WS,
``runs_final_20261023/weblane.sh``), mapped onto the new API lane (PR #220,
same observable contract):

- old WS ``chat_message`` send  -> ``POST /studies/{id}/run`` (the CLI run
  flag set verbatim as ``RunStudyBody``; built mechanically from the shared
  ``LiveRunnerConfig`` — no second validation path, the API lane reuses
  ``coscientist.cli.run._async_main``);
- old processing/idle statuses  -> ``WS /studies/{id}/stream?after=N`` frames
  (the study journal ``EventWire`` rows, JSONL-logged under
  ``driver_out_dir``);
- old ``final_response``        -> the ``StudyFinalized`` verdict event plus
  ``GET /studies/{id}/run`` (run status + the ``run_study`` summary).

Subprocess isolation: the API server (the system under test) runs as its own
``python -m coscientist.api.serve`` process group (every flag explicit from
config — uvicorn is the launcher's runtime dep, as for ``run_web.sh``); the
harness only observes via HTTP/WS (``httpx`` / ``websockets`` are lazy
imports — runtime-only deps supplied by the environment, repo dependency
surface unchanged).

Honest outcome mapping (no verdict invention):

- run terminal ``done`` + served summary -> ``RunnerSuccess`` via the SHARED
  ``success_from_summary`` (verdict / quality / usage, the CLI-lane mapping);
- run terminal refused/failed AFTER ``StudyFinalized`` (the API lane serves
  no summary for a refused run) -> ``RunnerSuccess`` with the verdict from
  the journal events and quality from the shared ``observe_quality`` fold;
  usage is genuinely unobservable on this path -> ``()`` (never estimated);
- observation window closed before ``StudyFinalized`` (stream timeout)
  -> ``RunnerSuccess`` with status ``"timeout"`` and the partial journal
  observation (a HER-comparable observed outcome, like the old driver's
  timed-out runs);
- POST refusal, server death, missing lazy deps, or a lane refusal before
  closure -> typed ``RunnerRefuse`` (infra / wiring, never a verdict).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from bench.drivers.journal import observe_quality
from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    UsageRecorded,
)
from bench.runsummary import success_from_summary
from coscientist.core.events.ids import study_id_from_idempotency_key

if TYPE_CHECKING:
    from bench.live_runner import LiveRunnerConfig

# Transport plumbing (mirrors bench/lab/driver/webdrive.py), not benchmark
# semantics: retry cadence while the server accepts connections, and the
# bounded poll for GET /run to turn terminal after the final event.
_CONNECT_RETRY_SLEEP_S = 0.25
_TERMINAL_POLL_BUDGET_S = 10.0
_TERMINAL_POLL_STEP_S = 0.05


# Structural surfaces of the lane's lazily-imported HTTP/WS transport modules
# (``post_run`` / ``collect_events`` / ``get_run_terminal`` take the module as
# an argument so ``_drive`` keeps importing them lazily and refusing typed
# when the runtime dep is absent). httpx is a repo extra, so mypy checks the
# real module against ``_HttpxTransport`` at the ``_drive`` call sites;
# websockets is launcher-supplied (not a repo dependency), so
# ``_WebsocketsTransport`` pins the documented usage shape only.
class _HttpResponse(Protocol):
    """One HTTP response: status code, body text, decoded body."""

    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    def json(self) -> object: ...


class _HttpClient(Protocol):
    """One ``httpx.AsyncClient`` connection scope as the lane uses it."""

    async def post(self, url: str, *, json: object) -> _HttpResponse: ...

    async def get(self, url: str) -> _HttpResponse: ...

    async def __aenter__(self) -> _HttpClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _HttpxTransport(Protocol):
    """The ``httpx`` module narrowed to the lane's surface."""

    def AsyncClient(self, *, base_url: str, timeout: float) -> _HttpClient: ...

    @property
    def HTTPError(self) -> type[Exception]: ...


class _WsConnection(Protocol):
    """One WS stream: frames arrive as ``str | bytes`` until closed."""

    async def recv(self) -> str | bytes: ...


class _WsConnect(Protocol):
    """One ``websockets.connect(...)`` connection scope."""

    async def __aenter__(self) -> _WsConnection: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _WebsocketsTransport(Protocol):
    """The ``websockets`` module narrowed to the lane's surface."""

    def connect(self, uri: str, *, open_timeout: float, close_timeout: float) -> _WsConnect: ...

    @property
    def WebSocketException(self) -> type[Exception]: ...


class WebDriver:
    """Drive one study through the API lane against a uvicorn subprocess."""

    def run(
        self,
        task: BenchTask,
        *,
        study_id: str,
        input_paths: tuple[Path, ...],
        config: LiveRunnerConfig,
    ) -> RunnerSuccess | RunnerRefuse:
        # The API lane enforces the core study-id format (require_study_id);
        # the bench study id maps onto the deterministic idempotent id from the
        # core helper — same input, same journal, reruns stay refused.
        api_study_id = study_id_from_idempotency_key(study_id)
        journal_dir = config.data_root / api_study_id
        if journal_dir.exists():
            return RunnerRefuse(
                kind="refused",
                reason_code="study_journal_exists",
                detail=f"refusing to overwrite an existing study journal: {journal_dir}",
            )
        out_dir = config.driver_out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        events_path = out_dir / f"{api_study_id}_events.jsonl"
        summary_path = out_dir / f"{api_study_id}_summary.json"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coscientist.api.serve",
                "--data-root",
                str(config.data_root),
                "--catalog",
                str(config.catalog),
                "--host",
                config.web_host,
                "--port",
                str(config.web_port),
                "--stream-poll-interval-s",
                str(config.web_stream_poll_interval_s),
                "--run-wait-s",
                str(config.web_run_wait_s),
            ],
            cwd=str(config.subprocess_cwd),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started = time.perf_counter()
        try:
            return self._drive(
                task,
                study_id=api_study_id,
                input_paths=input_paths,
                config=config,
                server=server,
                events_path=events_path,
                summary_path=summary_path,
                started=started,
            )
        finally:
            _stop_server(server)

    def _drive(
        self,
        task: BenchTask,
        *,
        study_id: str,
        input_paths: tuple[Path, ...],
        config: LiveRunnerConfig,
        server: subprocess.Popen[bytes],
        events_path: Path,
        summary_path: Path,
        started: float,
    ) -> RunnerSuccess | RunnerRefuse:
        try:
            import httpx
        except ImportError:
            return RunnerRefuse(
                kind="refused",
                reason_code="web_driver_deps_missing",
                detail="web driver requires httpx (repo mcp extra) — not importable",
            )
        if server.poll() is not None:
            return RunnerRefuse(
                kind="refused",
                reason_code="web_server_died",
                detail=f"api serve exited rc={server.returncode} before the run POST",
            )
        body = run_body(config, task.prompt, input_paths)
        status_code, text = post_run(httpx, config, study_id, body)
        if status_code != 200:
            return RunnerRefuse(
                kind="refused",
                reason_code="web_post_refused",
                detail=f"POST /studies/{study_id}/run -> {status_code}: {text[:400]}",
            )
        try:
            import websockets
        except ImportError:
            return RunnerRefuse(
                kind="refused",
                reason_code="web_driver_deps_missing",
                detail="web driver requires websockets (launcher-supplied runtime dep) — not importable",
            )
        events, timed_out = collect_events(websockets, config, study_id, events_path)
        final_event = next((event for event in events if is_final_event(event)), None)
        run_status = get_run_terminal(httpx, config, study_id)
        elapsed = time.perf_counter() - started
        write_summary(summary_path, study_id, elapsed, events, final_event, run_status, timed_out)
        if final_event is not None:
            return self._closed_outcome(task, study_id, events, final_event, run_status)
        if timed_out:
            # Observation window closed before the study finalized: an observed
            # mid-way death — HER-comparable, never a refused verdict.
            return observed_outcome("timeout", events, usage=())
        detail = str((run_status or {}).get("detail", "no run status"))
        return RunnerRefuse(
            kind="refused",
            reason_code="web_run_refused",
            detail=f"web lane ended without StudyFinalized (study {study_id!r}): {detail}",
        )

    def _closed_outcome(
        self,
        task: BenchTask,
        study_id: str,
        events: list[dict[str, object]],
        final_event: Mapping[str, object],
        run_status: dict[str, object] | None,
    ) -> RunnerSuccess | RunnerRefuse:
        run_state = str((run_status or {}).get("run_status", ""))
        summary = (run_status or {}).get("summary")
        if run_state == "done" and isinstance(summary, Mapping):
            # The lane served the run summary: the exact CLI-lane mapping.
            try:
                return success_from_summary(task, summary)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, ValidationError):
                return RunnerRefuse(
                    kind="refused", reason_code="summary_unreadable",
                    detail="served run summary failed the shared mapping",
                )
        # Refused/failed AFTER closure (the API lane serves no summary for a
        # refused run): verdict from the journal, quality from the shared
        # observer fold, usage unobservable -> ().
        observed = observe_quality(events)
        verdict = observed["verdict"] or verdict_of(final_event)
        if verdict is None:
            return RunnerRefuse(
                kind="refused",
                reason_code="web_run_refused",
                detail=f"study {study_id!r} closed with no readable verdict (run_status={run_state!r})",
            )
        return RunnerSuccess(
            kind="success",
            usage_recorded=(),
            quality=QualityObservation(
                status=str(verdict),
                exp_tasks=observed["exp_tasks"],
                evidence=evidence_from(observed),
                record_result=observed["record_result"],
            ),
        )


def _stop_server(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        if server.stderr is not None:
            server.stderr.close()
        return
    server.terminate()
    try:
        server.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        import os
        import signal

        try:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        server.wait(timeout=5.0)
    if server.stderr is not None:
        server.stderr.close()


def run_body(
    config: LiveRunnerConfig,
    task_text: str,
    input_paths: tuple[Path, ...],
) -> dict[str, object]:
    """``POST /studies/{id}/run`` body: the CLI run flag set, verbatim.

    Every field is required by ``RunStudyBody`` (explicit ``null``/``[]``
    where the CLI flag is optional) — built mechanically from the shared
    config, the same fields ``build_run_argv`` maps onto CLI flags.
    """
    return {
        "task_text": task_text,
        "owner": config.owner,
        "deadline_s": config.deadline_s,
        "lease_ttl_s": config.lease_ttl_s,
        "catalog_version": config.catalog_version,
        "catalog_path": str(config.catalog),
        "profile": config.profile,
        "tool_catalog_version": config.tool_catalog_version,
        "llm_config_path": str(config.llm_config),
        "planner_model": config.planner_model,
        "executor_model": config.executor_model,
        "tz_spec_model": config.tz_spec_model,
        "tools_json_path": str(config.tools_json) if config.tools_json is not None else None,
        "mcp_tool_bundle_path": str(config.mcp_tool_bundle) if config.mcp_tool_bundle is not None else None,
        "tool_call_args_path": str(config.mcp_tool_call_args) if config.mcp_tool_call_args is not None else None,
        "coder_backend": config.coder_backend,
        "coder_jobs_path": str(config.coder_jobs) if config.coder_jobs is not None else None,
        "wired_roles": list(config.wired_roles),
        "task_input_paths": [str(path) for path in input_paths],
        "require_hypotheses": config.require_hypotheses,
        "confirmations_needed": config.confirmations_needed,
        "refutations_needed": config.refutations_needed,
        "refutation_priority": config.refutation_priority,
        "supported_needed": config.supported_needed,
        "max_expansions": config.max_expansions,
        "finalize_reserve_s": config.finalize_reserve_s,
        "rng_seed": config.rng_seed,
        "max_llm_calls": config.max_llm_calls,
        "max_tokens": config.max_tokens,
        "max_tool_calls": config.max_tool_calls,
        "max_ticks": config.max_ticks,
        "hitl_mode": config.hitl_mode,
    }


def post_run(
    httpx: _HttpxTransport,
    config: LiveRunnerConfig,
    study_id: str,
    body: Mapping[str, object],
) -> tuple[int, str]:
    """POST /studies/{id}/run with connect retries; returns (status_code, text)."""
    import asyncio

    async def _post() -> tuple[int, str]:
        base_url = f"http://{config.web_host}:{config.web_port}"
        last_error = "unknown"
        for attempt in range(config.web_connect_retries + 1):
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=config.web_post_timeout_s) as client:
                    response = await client.post(f"/studies/{study_id}/run", json=dict(body))
                    return response.status_code, response.text
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < config.web_connect_retries:
                    await asyncio.sleep(_CONNECT_RETRY_SLEEP_S)
        return 0, f"connect_failed:{last_error}"

    return asyncio.run(_post())


def collect_events(
    websockets: _WebsocketsTransport,
    config: LiveRunnerConfig,
    study_id: str,
    events_path: Path,
) -> tuple[list[dict[str, object]], bool]:
    """Stream WS events until ``StudyFinalized`` or the observation budget ends.

    Reconnects resume ``?after=<last seq>`` — the journal is never replayed
    (no duplicate log lines, no inflated event counts). Returns
    ``(events, timed_out)``; every frame is JSONL-logged to ``events_path``.
    """
    import asyncio

    async def _collect() -> tuple[list[dict[str, object]], bool]:
        base = f"http://{config.web_host}:{config.web_port}".replace("http", "ws", 1)
        deadline = time.monotonic() + config.web_stream_timeout_s
        events: list[dict[str, object]] = []
        timed_out = False
        got_final = False
        after_seq = 0
        with events_path.open("w", encoding="utf-8") as events_out:
            while not got_final and not timed_out:
                url = f"{base}/studies/{study_id}/stream?after={after_seq}"
                try:
                    async with websockets.connect(
                        url,
                        open_timeout=max(1.0, deadline - time.monotonic()),
                        close_timeout=1.0,
                    ) as ws:
                        while not got_final:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0.0:
                                timed_out = True
                                break
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                            except asyncio.TimeoutError:
                                timed_out = True
                                break
                            event = json.loads(raw)
                            if not isinstance(event, dict):
                                continue
                            seq = event.get("seq")
                            if isinstance(seq, int) and seq <= after_seq:
                                continue  # replay guard: never double-log a seq
                            events.append(event)
                            events_out.write(json.dumps(event, ensure_ascii=False) + "\n")
                            events_out.flush()
                            if isinstance(seq, int):
                                after_seq = seq
                            if event.get("type") == "StudyFinalized":
                                got_final = True
                except (websockets.WebSocketException, OSError, TimeoutError):
                    # Dial refusals / open timeouts and mid-stream resets retry
                    # within the budget; the next attempt resumes after the
                    # last seen seq (same cadence as the lab webdrive driver).
                    await asyncio.sleep(_CONNECT_RETRY_SLEEP_S)
                if time.monotonic() >= deadline:
                    timed_out = True
        return events, timed_out

    return asyncio.run(_collect())


def get_run_terminal(
    httpx: _HttpxTransport,
    config: LiveRunnerConfig,
    study_id: str,
) -> dict[str, object] | None:
    """Poll GET /studies/{id}/run until terminal (bounded; ``None`` on error)."""
    import asyncio

    async def _get() -> dict[str, object] | None:
        base_url = f"http://{config.web_host}:{config.web_port}"
        deadline = time.monotonic() + _TERMINAL_POLL_BUDGET_S
        while True:
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                    response = await client.get(f"/studies/{study_id}/run")
            except httpx.HTTPError:
                return None
            if response.status_code != 200:
                return None
            loaded = response.json()
            if not isinstance(loaded, dict):
                return None
            if loaded.get("run_status") != "running":
                return loaded
            if time.monotonic() >= deadline:
                return loaded
            await asyncio.sleep(_TERMINAL_POLL_STEP_S)

    return asyncio.run(_get())


def is_final_event(event: Mapping[str, object]) -> bool:
    return event.get("type") == "StudyFinalized"


def verdict_of(event: Mapping[str, object]) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        verdict = payload.get("verdict")
        if isinstance(verdict, str) and verdict:
            return verdict
    return None


def evidence_from(observed: Mapping[str, Any]) -> tuple[EvidenceOutcomeCount, ...]:
    return tuple(
        EvidenceOutcomeCount(outcome=row["outcome"], count=row["count"]) for row in observed["evidence"]
    )


def observed_outcome(
    status: str,
    events: list[dict[str, object]],
    *,
    usage: tuple[UsageRecorded, ...],
) -> RunnerSuccess:
    """``RunnerSuccess`` from the journal observation alone (no served summary).

    Used when the lane cannot serve the run summary: the observation window
    closed before closure (status ``"timeout"``) or usage is unobservable.
    Quality is the shared ``observe_quality`` fold — never estimated.
    """
    observed = observe_quality(events)
    return RunnerSuccess(
        kind="success",
        usage_recorded=usage,
        quality=QualityObservation(
            status=status,
            exp_tasks=observed["exp_tasks"],
            evidence=evidence_from(observed),
            record_result=observed["record_result"],
        ),
    )


def write_summary(
    summary_path: Path,
    study_id: str,
    elapsed_s: float,
    events: list[dict[str, object]],
    final_event: Mapping[str, object] | None,
    run_status: dict[str, object] | None,
    timed_out: bool,
) -> None:
    """Summary JSON (old ``webdrive_result.json`` shape) under ``driver_out_dir``."""
    final: dict[str, object] = {}
    if final_event is not None:
        final["verdict"] = verdict_of(final_event)
    if run_status is not None:
        final["run_status"] = run_status.get("run_status")
        summary = run_status.get("summary")
        if isinstance(summary, Mapping):
            final["summary"] = dict(summary)
        detail = run_status.get("detail")
        if isinstance(detail, str):
            final["detail"] = detail
    if timed_out:
        final["timed_out"] = True
    summary_path.write_text(
        json.dumps(
            {
                "study_id": study_id,
                "elapsed_s": round(elapsed_s, 3),
                "n_events": len(events),
                "final": final,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
