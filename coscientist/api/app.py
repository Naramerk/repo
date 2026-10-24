"""FastAPI surface for DESIGN §9 (create_app + injected deps; no silent defaults)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from coscientist.api.cancel import require_cancel_by
from coscientist.api.errors import ApiRefuse
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
    """Composition-root bindings. Every field required — no defaults."""

    studies: StudyService
    event_store_for: Callable[[str], EventStore]
    cancel_study: Callable[[str, str], Awaitable[dict[str, str]]]
    stream_poll_interval_s: float


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str


def _http_error(exc: ApiRefuse | StudyNotFound) -> HTTPException:
    if isinstance(exc, StudyNotFound):
        return HTTPException(status_code=404, detail=exc.reason)
    return HTTPException(status_code=400, detail=exc.reason)


def create_app(deps: AppDeps) -> FastAPI:
    """Build the study API. ``deps.stream_poll_interval_s`` validated at create."""
    poll_s = require_poll_interval_s(deps.stream_poll_interval_s)
    app = FastAPI(title="coscientist-api")
    app.state.deps = deps
    app.state.stream_poll_interval_s = poll_s

    @app.get("/studies/{study_id}")
    def get_study_route(study_id: str) -> dict[str, Any]:
        try:
            return get_study(deps.studies, study_id).model_dump(mode="json")
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.get("/studies/{study_id}/events")
    async def get_events_route(
        study_id: str,
        after: Annotated[int, Query()],
    ) -> list[dict[str, Any]]:
        try:
            sid = require_study_id(study_id)
            store = deps.event_store_for(sid)
            wires = await list_study_events(store, study_id=sid, after=after)
            return [w.model_dump(mode="json") for w in wires]
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.post("/studies/{study_id}/cancel")
    async def cancel_route(study_id: str, body: CancelBody) -> dict[str, str]:
        try:
            sid = require_study_id(study_id)
            return await deps.cancel_study(sid, require_cancel_by(body.by))
        except (ApiRefuse, StudyNotFound) as exc:
            raise _http_error(exc) from exc

    @app.websocket("/studies/{study_id}/stream")
    async def stream_route(websocket: WebSocket, study_id: str) -> None:
        after_raw = websocket.query_params.get("after")
        try:
            if after_raw is None:
                raise ApiRefuse("after_query_required")
            if after_raw != after_raw.strip():
                raise ApiRefuse("after_query_whitespace")
            after_seq = require_after_seq(int(after_raw, 10))
            sid = require_study_id(study_id)
            store = deps.event_store_for(sid)
        except ApiRefuse as exc:
            await websocket.close(code=1008, reason=exc.reason[:120])
            return  # audit-ok[except_silent_return]: refuse query before WS accept
        except StudyNotFound as exc:
            await websocket.close(code=1008, reason=exc.reason[:120])
            return  # audit-ok[except_silent_return]: missing study before WS accept
        except (ValueError, TypeError) as exc:
            detail = f"after_query_invalid:{type(exc).__name__}"
            await websocket.close(code=1008, reason=detail[:120])
            return  # audit-ok[except_silent_return]: bad after query before WS accept
        try:
            await stream_study_events(
                websocket,
                store,
                study_id=sid,
                after=after_seq,
                poll_interval_s=app.state.stream_poll_interval_s,
            )
        except WebSocketDisconnect:
            return  # audit-ok[except_silent_return]: client disconnected mid-stream
        except (ApiRefuse, StudyNotFound) as exc:
            await websocket.close(code=1008, reason=exc.reason[:120])

    return app
