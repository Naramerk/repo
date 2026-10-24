"""FastAPI surface for DESIGN §9 (create_app + injected deps; no silent defaults)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, WebSocket
from pydantic import BaseModel, ConfigDict
from starlette.websockets import WebSocketDisconnect

from coscientist.api.errors import ApiRefuse, WsCloseReasonRefuse
from coscientist.api.stream import require_poll_interval_s, stream_study_events
from coscientist.api.views import (
    get_study,
    list_study_events,
    parse_after_query,
    require_study_id,
)
from coscientist.core.events.errors import StudyNotFound
from coscientist.core.events.ports import EventStore, StudyService

# RFC 6455 §5.5.1 / Starlette: close reason payload ≤ 123 bytes UTF-8.
WS_CLOSE_REASON_MAX_BYTES = 123


@dataclass(frozen=True, slots=True)
class AppDeps:
    """Composition-root bindings. Every field required — no defaults.

    ``cancel_study``: use ``bind_cancel_study(data_root, catalog=...)``.
    ``stream_sleep``: inject clock (tests: yield-only; prod: ``asyncio.sleep``).
    """

    studies: StudyService
    event_store_for: Callable[[str], EventStore]
    cancel_study: Callable[[str, str], Awaitable[Mapping[str, str]]]
    stream_poll_interval_s: float
    stream_sleep: Callable[[float], Awaitable[None]]


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    by: str


def _http_error(exc: ApiRefuse | StudyNotFound) -> HTTPException:
    if isinstance(exc, StudyNotFound):
        return HTTPException(status_code=404, detail=exc.reason)
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
    if isinstance(exc, BaseException):
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
            store = deps.event_store_for(sid)
            after_seq = parse_after_query(after)
            wires = await list_study_events(store, study_id=sid, after=after_seq)
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
        try:
            after_seq, sid, store = _stream_target(deps, study_id, after_raw)
        except (ApiRefuse, StudyNotFound) as exc:
            await _ws_refuse(websocket, exc.reason)
        else:
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

    return app
