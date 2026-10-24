"""FastAPI surface for DESIGN §9 (create_app + injected deps; no silent defaults)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, WebSocket
from pydantic import BaseModel, ConfigDict
from starlette.websockets import WebSocketDisconnect

from coscientist.api.errors import AfterSeqRefuse, ApiRefuse
from coscientist.api.stream import require_poll_interval_s, stream_study_events
from coscientist.api.views import (
    get_study,
    list_study_events,
    require_after_seq,
    require_study_id,
)
from coscientist.core.events.errors import StudyNotFound
from coscientist.core.events.ports import EventStore, StudyService


@dataclass(frozen=True, slots=True)
class AppDeps:
    """Composition-root bindings. Every field required — no defaults.

    ``cancel_study``: use ``bind_cancel_study(data_root, catalog=...)``.
    """

    studies: StudyService
    event_store_for: Callable[[str], EventStore]
    cancel_study: Callable[[str, str], Awaitable[Mapping[str, str]]]
    stream_poll_interval_s: float


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    by: str


def _http_error(exc: ApiRefuse | StudyNotFound) -> HTTPException:
    if isinstance(exc, StudyNotFound):
        return HTTPException(status_code=404, detail=exc.reason)
    return HTTPException(status_code=400, detail=exc.reason)


async def _ws_refuse(websocket: WebSocket, reason: str) -> None:
    """Close with 1008; caller returns — peer sees the reason."""
    await websocket.close(code=1008, reason=reason[:120])


async def _await_cancelled(task: asyncio.Task[None]) -> None:
    """Cancel ``task`` and wait without ``suppress`` / silent pass."""
    if not task.done():
        task.cancel()
    outcomes = await asyncio.gather(task, return_exceptions=True)
    err = outcomes[0]
    if isinstance(err, BaseException) and type(err) is not asyncio.CancelledError:
        raise err


def create_app(deps: AppDeps) -> FastAPI:
    """Build the study API. ``deps.stream_poll_interval_s`` validated at create."""
    poll_s = require_poll_interval_s(deps.stream_poll_interval_s)
    app = FastAPI(title="coscientist-api")

    @app.get("/studies/{study_id}")
    def get_study_route(study_id: str) -> Mapping[str, object]:
        try:
            return get_study(deps.studies, study_id).model_dump(mode="json")
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.get("/studies/{study_id}/events")
    async def get_events_route(
        study_id: str,
        after: Annotated[int, Query()],
    ) -> list[Mapping[str, object]]:
        try:
            sid = require_study_id(study_id)
            store = deps.event_store_for(sid)
            wires = await list_study_events(store, study_id=sid, after=after)
            return [w.model_dump(mode="json") for w in wires]
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.post("/studies/{study_id}/cancel")
    async def cancel_route(study_id: str, body: CancelBody) -> Mapping[str, str]:
        # Validation owned by cancel_study (bind_cancel_study → post_study_cancel).
        try:
            return await deps.cancel_study(study_id, body.by)
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.websocket("/studies/{study_id}/stream")
    async def stream_route(websocket: WebSocket, study_id: str) -> None:
        after_raw = websocket.query_params.get("after")
        refuse_reasons: list[str] = []
        validated: list[tuple[int, str, EventStore]] = []
        try:
            if after_raw is None:
                raise AfterSeqRefuse("after_query_required")
            if after_raw != after_raw.strip():
                raise AfterSeqRefuse("after_query_whitespace")
            try:
                after_int = int(after_raw, 10)
            except ValueError as exc:
                raise AfterSeqRefuse(f"after_query_invalid:{type(exc).__name__}") from exc
            after_seq = require_after_seq(after_int)
            sid = require_study_id(study_id)
            store = deps.event_store_for(sid)
            validated.append((after_seq, sid, store))
        except ApiRefuse as exc:
            refuse_reasons.append(exc.reason)
        except StudyNotFound as exc:
            refuse_reasons.append(exc.reason)
        if refuse_reasons:
            await _ws_refuse(websocket, refuse_reasons[0])
            return
        after_seq, sid, store = validated[0]

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
        stream_refuse: list[str] = []
        try:
            await stream_study_events(
                websocket,
                store,
                study_id=sid,
                after=after_seq,
                poll_interval_s=poll_s,
                sleep=asyncio.sleep,
                is_disconnected=disconnected.is_set,
            )
        except ApiRefuse as exc:
            stream_refuse.append(exc.reason)
        except StudyNotFound as exc:
            stream_refuse.append(exc.reason)
        finally:
            await _await_cancelled(watcher)
        if stream_refuse:
            await _ws_refuse(websocket, stream_refuse[0])

    return app
