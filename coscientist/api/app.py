"""FastAPI surface for DESIGN §9 (create_app + injected deps; no silent defaults)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, WebSocket
from pydantic import BaseModel, ConfigDict
from starlette.websockets import WebSocketDisconnect

from coscientist.api.cancel import post_study_cancel
from coscientist.api.console import register_console_routes, web_dir
from coscientist.api.errors import ApiRefuse, StudyRunFailed, WsCloseReasonRefuse
from coscientist.api.hitl_routes import register_hitl_routes
from coscientist.api.run import RunStudyBody, StudyRunService
from coscientist.api.stream import require_poll_interval_s, stream_study_events
from coscientist.api.study_service import DataRootStudyService, bind_event_store_for
from coscientist.api.views import (
    get_study,
    list_study_events,
    parse_after_query,
    require_study_id,
)
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import StudyNotFound
from coscientist.core.events.ports import EventStore, StudyService
from coscientist.core.execution.domain import Catalog

# RFC 6455 §5.5.1 / Starlette: close reason payload ≤ 123 bytes UTF-8.
WS_CLOSE_REASON_MAX_BYTES = 123


@dataclass(frozen=True, slots=True)
class AppDeps:
    """Composition-root bindings. Every field required — no defaults.

    Prefer ``bind_app_deps`` for the data_root + SQLite path.
    ``stream_sleep``: inject clock (tests: yield-only; prod: ``asyncio.sleep``).
    """

    studies: StudyService
    event_store_for: Callable[[str], EventStore]
    cancel_study: Callable[[str, str], Awaitable[Mapping[str, str]]]
    run_studies: StudyRunService
    stream_poll_interval_s: float
    stream_sleep: Callable[[float], Awaitable[None]]
    data_root: Path


def bind_app_deps(
    data_root: Path | str,
    *,
    catalog: Catalog,
    clock: Clock,
    stream_poll_interval_s: float,
    stream_sleep: Callable[[float], Awaitable[None]],
    run_wait_s: float,
) -> AppDeps:
    """Wire ``DataRootStudyService`` + SQLite ``EventStore`` + cancel + run into ``AppDeps``.

    All parameters required (fail-closed). Does not invent poll/sleep/clock
    defaults. ``run_wait_s`` bounds both start-visibility and interrupt waits
    of ``StudyRunService``. Cancel is run-aware: an API-started live run is
    interrupted (releasing its lease) before the journal cancel runs.
    """
    root = Path(data_root)
    run_studies = StudyRunService(
        root,
        catalog=catalog,
        clock=clock,
        wait_s=run_wait_s,
        poll_interval_s=stream_poll_interval_s,
        sleep=stream_sleep,
    )

    async def cancel_study(study_id: str, by: str) -> dict[str, str]:
        await run_studies.interrupt(study_id)
        return await post_study_cancel(root, study_id, catalog=catalog, by=by)

    return AppDeps(
        studies=DataRootStudyService(root, catalog=catalog),
        event_store_for=bind_event_store_for(root, clock=clock),
        cancel_study=cancel_study,
        run_studies=run_studies,
        stream_poll_interval_s=stream_poll_interval_s,
        stream_sleep=stream_sleep,
        data_root=root,
    )


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    by: str


def _http_error(exc: ApiRefuse | StudyNotFound) -> HTTPException:
    if isinstance(exc, StudyNotFound):
        return HTTPException(status_code=404, detail=exc.reason)
    if isinstance(exc, StudyRunFailed):
        return HTTPException(status_code=500, detail=exc.reason)
    return HTTPException(status_code=400, detail=exc.reason)


def encode_ws_close_reason(reason: str) -> str:
    """Validate close-reason UTF-8 byte length; refuse instead of truncating."""
    size = len(reason.encode("utf-8"))
    if size > WS_CLOSE_REASON_MAX_BYTES:
        limit = WS_CLOSE_REASON_MAX_BYTES
        msg = f"ws_close_reason_exceeds_{limit}_bytes_got_{size}"
        raise WsCloseReasonRefuse(msg)
    return reason


async def _ws_refuse(websocket: WebSocket, reason: str) -> None:
    """Close with 1008; caller returns — peer sees the reason."""
    await websocket.close(code=1008, reason=encode_ws_close_reason(reason))


async def _await_cancelled(task: asyncio.Task[None]) -> None:
    """Cancel ``task`` and wait without list indexing / ``suppress``."""
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        raise exc


def _stream_target(
    deps: AppDeps,
    study_id: str,
    after_raw: str | None,
) -> tuple[int, str, EventStore]:
    """Validate path + after; return ``(after_seq, sid, store)`` or raise."""
    after_seq = parse_after_query(after_raw)
    sid = require_study_id(study_id)
    store = deps.event_store_for(sid)
    return after_seq, sid, store


def create_app(deps: AppDeps) -> FastAPI:
    """Build the study API. ``deps.stream_poll_interval_s`` validated at create."""
    poll_s = require_poll_interval_s(deps.stream_poll_interval_s)
    app = FastAPI(title="coscientist-api")
    register_hitl_routes(app, data_root=deps.data_root, runs=deps.run_studies)
    register_console_routes(
        app,
        data_root=deps.data_root,
        web_dir=web_dir(),
        stream_poll_interval_s=poll_s,
        stream_sleep=deps.stream_sleep,
    )

    @app.get("/studies/{study_id}")
    def get_study_route(study_id: str) -> Mapping[str, object]:
        try:
            return get_study(deps.studies, study_id).model_dump(mode="json")
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.get("/studies/{study_id}/events")
    async def get_events_route(
        study_id: str,
        after: Annotated[str, Query()],
    ) -> list[Mapping[str, object]]:
        try:
            sid = require_study_id(study_id)
            after_seq = parse_after_query(after)
            store = deps.event_store_for(sid)
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc
        try:
            wires = await list_study_events(store, study_id=sid, after=after_seq)
            return [w.model_dump(mode="json") for w in wires]
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc
        finally:
            await store.close()

    @app.post("/studies/{study_id}/cancel")
    async def cancel_route(study_id: str, body: CancelBody) -> Mapping[str, str]:
        # Validation owned by cancel_study (bind_cancel_study → post_study_cancel).
        try:
            return await deps.cancel_study(study_id, body.by)
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.post("/studies/{study_id}/run")
    async def post_run_route(study_id: str, body: RunStudyBody) -> Mapping[str, object]:
        # Validation owned by StudyRunService + the reused CLI loaders.
        try:
            return await deps.run_studies.post_run(study_id, body)
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.get("/studies/{study_id}/run")
    def get_run_route(study_id: str) -> Mapping[str, object]:
        try:
            return deps.run_studies.run_status(study_id)
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.websocket("/studies/{study_id}/stream")
    async def stream_route(websocket: WebSocket, study_id: str) -> None:
        after_raw = websocket.query_params.get("after")
        try:
            after_seq, sid, store = _stream_target(deps, study_id, after_raw)
        except (ApiRefuse, StudyNotFound) as exc:
            await _ws_refuse(websocket, exc.reason)
        else:
            try:
                await websocket.accept()
                disconnected = asyncio.Event()

                async def watch_disconnect() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                disconnected.set()
                                return
                    except WebSocketDisconnect:
                        disconnected.set()

                watcher = asyncio.create_task(watch_disconnect())
                try:
                    # list_study_events(study_id=sid) — path id, not store-only.
                    await stream_study_events(
                        websocket,
                        store,
                        study_id=sid,
                        after=after_seq,
                        poll_interval_s=poll_s,
                        sleep=deps.stream_sleep,
                        is_disconnected=disconnected.is_set,
                    )
                except (ApiRefuse, StudyNotFound) as exc:
                    await _ws_refuse(websocket, exc.reason)
                finally:
                    await _await_cancelled(watcher)
            finally:
                await store.close()

    return app
