"""POST/GET ``/studies/{id}/run`` — start a study run over the web lane.

The whole CLI ``run`` path (config loaders, fail-closed validation, the
``run_study`` loop) is reused verbatim by calling ``coscientist.cli.run.
_async_main`` with an ``argparse.Namespace`` built 1:1 from the request body:
no second validation path, no scenario-fitted special cases. The API layer
only (a) refuses unknown/absent config refs with typed errors, (b) reserves
the study so a concurrent second run typed-refuses, and (c) exposes run
status + the final ``run_study`` summary. Status events are the study journal
itself — the existing WS stream (``/studies/{id}/stream``) already carries
them, and ``StudyFinalized`` is the final verdict/response event.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    model_serializer,
)

from coscientist.api.cancel import post_study_cancel
from coscientist.api.errors import StudyRunFailed, StudyRunRefuse
from coscientist.api.hitl import AnswerBody, HitlOwner, HitlRefuse
from coscientist.api.journal import safe_study_component
from coscientist.api.run_binding import record_active_journal, recorded_run_binding
from coscientist.api.stream import require_poll_interval_s
from coscientist.api.study_service import study_events_db
from coscientist.api.views import require_study_id
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import _async_main, describe_cli_capabilities
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import Catalog
from coscientist.core.execution.thresholds import (
    CONFIRMATIONS_NEEDED_GE,
    MAX_EXPANSIONS_GE,
    REFUTATIONS_NEEDED_GE,
    SUPPORTED_NEEDED_GE,
)

# Journal event that proves the run loop reached create_study (lease taken).
_START_EVENT = "StudyCreated"
# Cancel identity when the start budget expires with a visible journal.
_RUN_TIMEOUT_BY = "api-run-start-timeout"


class RunStudySettings(BaseModel):
    """``POST /studies/{id}/run`` request — the CLI ``run`` flag set, verbatim.

    Every field is required (explicit ``null``/``[]`` where the CLI flag is
    optional and absent) — no API-side defaults. Content validation lives in
    the CLI loaders (``_async_main`` → profile/catalog/LLM/surface parsers).
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    owner: str = Field(min_length=1)
    deadline_s: float = Field(gt=0.0)
    lease_ttl_s: float = Field(gt=0.0)
    catalog_version: StrictInt
    catalog_path: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    tool_catalog_version: StrictInt
    llm_config_path: str = Field(min_length=1)
    planner_model: str = Field(min_length=1)
    executor_model: str = Field(min_length=1)
    tz_spec_model: str = Field(min_length=1)
    tools_json_path: str | None
    mcp_tool_bundle_path: str | None
    tool_call_args_path: str | None
    coder_backend: Literal["bwrap", "local-python"] | None
    coder_jobs_path: str | None
    wired_roles: list[str]
    task_input_paths: list[str]
    require_hypotheses: StrictBool
    confirmations_needed: StrictInt = Field(ge=CONFIRMATIONS_NEEDED_GE)
    refutations_needed: StrictInt = Field(ge=REFUTATIONS_NEEDED_GE)
    refutation_priority: StrictBool
    supported_needed: StrictInt = Field(ge=SUPPORTED_NEEDED_GE)
    max_expansions: StrictInt = Field(ge=MAX_EXPANSIONS_GE)
    finalize_reserve_s: StrictInt
    rng_seed: StrictInt
    max_llm_calls: StrictInt
    max_tokens: StrictInt
    max_tool_calls: StrictInt
    max_ticks: StrictInt
    hitl_mode: Literal["interactive", "auto", "headless"]
    allowed_tool_ids: list[str] | None = None  # audit-ok[field_default]: historical payloads retain the trusted surface

    @model_serializer(mode="wrap")
    def _historical_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if self.allowed_tool_ids is None and "allowed_tool_ids" in payload:
            del payload["allowed_tool_ids"]
        return payload


class RunStudyBody(RunStudySettings):
    """Existing explicit run request: shared settings plus the human task."""

    task_text: str = Field(min_length=1)


def describe_run_capabilities(
    settings: RunStudySettings,
) -> tuple[dict[str, str], dict[str, tuple[str, tuple[str, ...]]]]:
    return describe_cli_capabilities(
        profile_name=settings.profile, tool_catalog_version=settings.tool_catalog_version,
        bundle_path=_optional_path(settings.mcp_tool_bundle_path),
    )


def require_run_wait_s(value: float) -> float:
    """Positive finite bound for waiting a run to become journal-visible."""
    if not math.isfinite(value) or value <= 0.0:
        raise StudyRunRefuse(f"run_wait_s_must_be_positive_finite_got_{value!r}")
    return value


def _optional_path(raw: str | None) -> Path | None:
    if raw is None:
        return None
    return Path(raw)


def _task_input_paths(raw: list[str]) -> list[Path]:
    return [Path(entry) for entry in raw]


def _namespace_from_body(
    body: RunStudyBody,
    *,
    sid: str,
    data_root: Path,
    task: Path,
) -> argparse.Namespace:
    """Body → CLI ``argparse.Namespace`` (attribute names are the CLI flags)."""
    return argparse.Namespace(
        task=task,
        data_root=data_root,
        deadline_s=body.deadline_s,
        catalog_version=body.catalog_version,
        catalog=Path(body.catalog_path),
        profile=body.profile,
        tool_catalog_version=body.tool_catalog_version,
        owner=body.owner,
        llm_config=Path(body.llm_config_path),
        planner_model=body.planner_model,
        executor_model=body.executor_model,
        tz_spec_model=body.tz_spec_model,
        tools_json=_optional_path(body.tools_json_path),
        mcp_tool_bundle=_optional_path(body.mcp_tool_bundle_path),
        tool_call_args=_optional_path(body.tool_call_args_path),
        allowed_tool_ids=body.allowed_tool_ids,
        coder_backend=body.coder_backend,
        coder_jobs=_optional_path(body.coder_jobs_path),
        wired_roles=json.dumps(body.wired_roles),
        study_id=sid,
        task_input=_task_input_paths(body.task_input_paths),
        require_hypotheses=body.require_hypotheses,
        confirmations_needed=body.confirmations_needed,
        refutations_needed=body.refutations_needed,
        refutation_priority=body.refutation_priority,
        supported_needed=body.supported_needed,
        max_expansions=body.max_expansions,
        finalize_reserve_s=body.finalize_reserve_s,
        lease_ttl_s=body.lease_ttl_s,
        rng_seed=body.rng_seed,
        max_llm_calls=body.max_llm_calls,
        max_tokens=body.max_tokens,
        max_tool_calls=body.max_tool_calls,
        max_ticks=body.max_ticks,
        hitl_mode=body.hitl_mode,
    )


def _require_config_refs(body: RunStudyBody) -> None:
    """File refs the CLI reads raw (no CliRefuse wrapper) — typed-refuse misses."""
    refs: tuple[tuple[str, str | None], ...] = (
        ("catalog_path", body.catalog_path),
        ("llm_config_path", body.llm_config_path),
        ("tools_json_path", body.tools_json_path),
        ("mcp_tool_bundle_path", body.mcp_tool_bundle_path),
        ("tool_call_args_path", body.tool_call_args_path),
        ("coder_jobs_path", body.coder_jobs_path),
    )
    missing: list[str] = []
    for name, raw in refs:
        if raw is not None and not Path(raw).is_file():
            missing.append(f"{name}={raw!r}")
    if missing:
        raise StudyRunRefuse(f"config_ref_not_found:{','.join(missing)}")


def _parse_summary(out: io.StringIO) -> dict[str, object] | None:
    """The single ``run_study`` summary JSON line, if the run wrote one."""
    raw = out.getvalue().strip()
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None  # audit-ok[except_silent_return]: non-JSON sink means no summary; run_status stays authoritative
    if not isinstance(loaded, dict):
        return None
    return {str(key): value for key, value in loaded.items()}


@dataclass
class _RunRecord:
    """One API-started run: its asyncio task, status, and captured summary."""

    study_id: str
    status: str
    detail: str | None
    summary: dict[str, object] | None
    task: asyncio.Task[None] | None
    control: HitlOwner | None
    active_study_id: str | None

    def finish(self, status: str, detail: str | None, out: io.StringIO) -> None:
        self.status = status
        self.detail = detail
        self.summary = _parse_summary(out)

    def mark(self, status: str, detail: str | None) -> None:
        """Terminal status without a summary parse (journal-driven outcome)."""
        self.status = status
        self.detail = detail


class StudyRunService:
    """Start/observe study runs under ``data_root`` (one live run per study).

    ``post_run`` reuses ``coscientist.cli.run._async_main`` for the entire CLI
    run path; this service adds only web-lane concerns: single-flight
    reservation, typed config-ref refusals, and run status. Journal events
    (streamed by the existing WS route) are the status/final-verdict surface.
    """

    def __init__(
        self,
        data_root: Path | str,
        *,
        catalog: Catalog,
        clock: Clock,
        wait_s: float,
        poll_interval_s: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._root = Path(data_root)
        self._catalog = catalog
        self._clock = clock
        self._wait_s = require_run_wait_s(wait_s)
        self._poll_s = require_poll_interval_s(poll_interval_s)
        self._sleep = sleep
        self._runs: dict[str, _RunRecord] = {}

    async def post_run(self, study_id: str, body: RunStudyBody) -> dict[str, object]:
        """Start a run; returns once the study journal is visible (or refused)."""
        sid = require_study_id(study_id)
        record = await self._require_startable(sid)
        try:
            _require_config_refs(body)
            task_file = self._write_task_file(sid, body.task_text)
            ns = _namespace_from_body(body, sid=sid, data_root=self._root, task=task_file)
            if body.hitl_mode == "interactive":
                record.control = HitlOwner(poll_s=self._poll_s, lease_ttl_s=body.lease_ttl_s)
            record.task = asyncio.create_task(self._execute(ns, record))
        except BaseException:  # audit-ok[broad_except]: claim release on pre-task failure; exception propagates
            del self._runs[sid]
            raise
        return await self._await_start(sid, record)

    def run_status(self, study_id: str) -> dict[str, object]:
        """GET /studies/{id}/run — status (+ summary/detail once terminal)."""
        sid = require_study_id(study_id)
        record = self._runs.get(sid)
        if record is None:
            _, db_path = study_events_db(self._root, sid)
            if not db_path.is_file():
                raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
            raise StudyRunRefuse("study_run_not_found")
        response: dict[str, object] = {"study_id": sid, "run_status": record.status}
        if record.summary is not None:
            response["summary"] = record.summary
        if record.detail is not None:
            response["detail"] = record.detail
        return response

    @property
    def start_wait_s(self) -> float:
        return self._wait_s

    def console_status(self, study_id: str) -> dict[str, object]:
        """Safe owner metadata for a root run or its current replan journal."""
        sid = safe_study_component(study_id)
        for record in self._runs.values():
            active = record.study_id
            if record.active_study_id is not None:
                active = record.active_study_id
            elif record.control is not None and record.control.active_study_id is not None:
                active = record.control.active_study_id
            if record.summary is not None:
                summary_sid = record.summary.get("study_id")
                if isinstance(summary_sid, str):
                    active = summary_sid
            if sid in (record.study_id, active):
                response: dict[str, object] = {
                    "study_id": record.study_id, "active_study_id": active,
                    "run_status": record.status, "can_cancel": record.status == "running",
                }
                if record.detail is not None:
                    response["detail"] = f"run_{record.status}"
                if record.summary is not None:
                    response["verdict"] = record.summary.get("verdict")
                return response
        raise StudyRunRefuse("study_run_not_found")

    def hitl_answer_target(self, study_id: str, request_id: str) -> str | None:
        """Return the strict API run ID, including for its journaled replan child."""
        for record in self._runs.values():
            if record.control is not None and record.control.available(study_id, request_id):
                return record.study_id
        return None

    async def answer_hitl(self, study_id: str, request_id: str, body: AnswerBody) -> dict[str, object]:
        sid = require_study_id(study_id)
        record = self._runs.get(sid)
        if record is not None and record.control is not None:
            waiting = record.control.waiting
            if waiting is not None and waiting[1] == request_id:
                return await record.control.submit(waiting[0], request_id, body)
        raise HitlRefuse("hitl_live_owner_unavailable")

    async def interrupt(self, study_id: str) -> str:
        """Cancel a live API-started run so a journal cancel can take the lease."""
        sid = require_study_id(study_id)
        record = self._runs.get(sid)
        if record is None:
            binding = recorded_run_binding(self._root, sid)
            if binding is not None:
                return binding[1]
            return sid
        if record.task is not None and not record.task.done():
            record.task.cancel()
            await asyncio.wait({record.task}, timeout=self._wait_s)
            if not record.task.done():
                raise StudyRunRefuse("study_run_interrupt_timeout")
        if record.active_study_id is not None:
            return record.active_study_id
        if record.control is not None and record.control.active_study_id is not None:
            return record.control.active_study_id
        return sid

    async def _require_startable(self, sid: str) -> _RunRecord:
        """Claim the single-flight slot; refuse races / re-runs (event-keyed).

        F5: the slot is claimed synchronously BEFORE the journal await — with
        the old check-then-await shape, a second same-sid POST could slip
        through while the first was suspended in ``_study_created``. The
        journal check reads the ``StudyCreated`` EVENT, not
        ``db_path.is_file()``: a run interrupted between store-open and
        ``create_study`` leaves an empty stub db that must stay re-runnable
        (such a stub has no terminal event either, so cancel cannot clear it
        — only the event key avoids the zombie).

        Returns the claimed record (the caller fills ``task``); the claim is
        released by the caller on any pre-task failure.
        """
        current = self._runs.get(sid)
        if current is not None and current.status == "running":
            raise StudyRunRefuse("study_run_in_progress")
        record = _RunRecord(sid, "running", None, None, None, None, None)
        self._runs[sid] = record
        try:
            created = await self._study_created(sid)
        except BaseException:  # audit-ok[broad_except]: claim release on journal-read failure; exception propagates
            del self._runs[sid]
            raise
        if created:
            del self._runs[sid]
            raise StudyRunRefuse("study_journal_already_exists")
        return record

    def _write_task_file(self, sid: str, task_text: str) -> Path:
        run_dir = self._root / sid / "run"
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            task_file = run_dir / "task.txt"
            task_file.write_text(task_text, encoding="utf-8")
        except OSError as exc:
            raise StudyRunRefuse(f"run_task_file_unwritable:{type(exc).__name__}") from exc
        return task_file

    async def _await_start(self, sid: str, record: _RunRecord) -> dict[str, object]:
        """Wait until ``StudyCreated`` is journal-visible or the task ends."""
        waited = 0.0
        while True:
            if record.task is not None and record.task.done():
                return self._terminal_response(record)
            if await self._study_created(sid):
                return {"study_id": sid, "run_status": "running"}
            if waited >= self._wait_s:
                break
            await self._sleep(self._poll_s)
            waited += self._poll_s
        return await self._timeout_resolution(sid, record)

    async def _timeout_resolution(self, sid: str, record: _RunRecord) -> dict[str, object]:
        """Start budget exhausted: cancel the run, then never orphan the journal.

        The run may have committed ``StudyCreated`` between the last poll and
        the cancel — refusing then would leave an open journal with no
        terminal event (re-runs refused forever). When the event is visible
        the study is cancelled via ``post_study_cancel`` (terminal, observable
        on the stream). When it is not, the run died pre-create: any stub db
        is re-runnable (re-run keys on the event), so a plain typed refuse is
        safe.
        """
        if record.task is not None:
            if record.task.done():
                # The run finished between the last poll and this check —
                # its typed outcome (refuse/fail detail) is the honest answer.
                return self._terminal_response(record)
            record.task.cancel()
            await asyncio.wait({record.task}, timeout=self._wait_s)
            if not record.task.done():
                # Cancellation stuck in cleanup (lease not yet released):
                # refuse typed instead of racing the journal cancel on a
                # lease the run still holds. Operator may retry after TTL.
                raise StudyRunRefuse("study_run_interrupt_timeout")
        if await self._study_created(sid):
            await post_study_cancel(self._root, sid, catalog=self._catalog, by=_RUN_TIMEOUT_BY)
            record.mark("cancelled", "study_run_start_timeout_cancelled")
            return {
                "study_id": sid,
                "run_status": "cancelled",
                "detail": "study_run_start_timeout_cancelled",
            }
        raise StudyRunRefuse("study_run_start_timeout")

    def _terminal_response(self, record: _RunRecord) -> dict[str, object]:
        # Run ended before the journal showed StudyCreated (config refusals,
        # intake refusals, crashes) — surface it as a typed HTTP outcome.
        if record.status == "done":
            response: dict[str, object] = {
                "study_id": record.study_id,
                "run_status": "done",
            }
            if record.summary is not None:
                response["summary"] = record.summary
            return response
        detail = record.detail
        if detail is None:
            detail = "unknown"
        if record.status == "failed":
            raise StudyRunFailed(f"study_run_failed:{detail}")
        if record.status == "cancelled":
            raise StudyRunRefuse(f"study_run_cancelled:{detail}")
        raise StudyRunRefuse(f"study_run_refused:{detail}")

    async def _study_created(self, sid: str) -> bool:
        _, db_path = study_events_db(self._root, sid)
        if not db_path.is_file():
            return False
        store = SqliteEventStore(sid, db_path, clock=self._clock, owner=None)
        try:
            events = await store.read_all()
        finally:
            await store.close()
        for event in events:
            if event.type == _START_EVENT:
                return True
        return False

    async def _tee_run_output(self, out: io.StringIO, study_id: str) -> None:
        """Mirror the captured CLI output to ``<study_id>.run.log`` while it runs.

        The StringIO sink is invisible to operators; persisting it (best-effort,
        ~2s granularity) makes retries/refusals debuggable after the fact. The
        final full flush happens on completion via the same poll loop's last
        write before cancellation.
        """
        path = self._root / f"{study_id}.run.log"
        written = -1
        try:
            while True:
                value = out.getvalue()
                if len(value) != written:
                    path.write_text(value, encoding="utf-8")
                    written = len(value)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            value = out.getvalue()
            if len(value) != written:
                path.write_text(value, encoding="utf-8")
            raise

    async def _execute(self, ns: argparse.Namespace, record: _RunRecord) -> None:
        """Run the CLI path to completion; capture typed status + summary."""
        out = io.StringIO()
        tee = asyncio.create_task(self._tee_run_output(out, record.study_id))

        def runtime_opened(sid: str) -> None:
            record_active_journal(self._root, record.study_id, sid)
            record.active_study_id = sid

        try:
            if record.control is None:
                code = await _async_main(ns, out=out, runtime_opened=runtime_opened)
            else:
                code = await _async_main(ns, out=out, control=record.control, runtime_opened=runtime_opened)
        except CliRefuse as exc:
            record.finish("refused", exc.detail, out)
        except asyncio.CancelledError:
            record.finish("cancelled", "study_run_interrupted", out)
            raise
        except BaseException as exc:  # audit-ok[broad_except]: run-task boundary; typed status capture
            record.finish("failed", f"{type(exc).__name__}: {exc}", out)
        else:
            if code == 0:
                record.finish("done", None, out)
            else:
                record.finish("refused", f"exit_code_{code}", out)
        finally:
            tee.cancel()
            if record.control is not None:
                record.control.close()
