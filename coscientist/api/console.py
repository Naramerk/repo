"""Read-only journal console; registrations identify CAS bytes, never graph IDs."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from anyio import create_task_group
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.websockets import WebSocket, WebSocketDisconnect

from coscientist.api.errors import ApiRefuse
from coscientist.api.views import event_to_wire, parse_after_query
from coscientist.core.events.store import stored_event_from_row

if TYPE_CHECKING:
    from fastapi import FastAPI


def web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


def safe_study_component(name: str) -> str:
    """Accept CLI and API journal IDs while refusing path components that escape."""
    if not name or name != name.strip() or name in (".", "..") or any(
        char in name for char in ("/", "\\", "\x00")
    ):
        raise ApiRefuse("study_dir_unsafe")
    return name


def study_path(root: Path, sid: str) -> Path:
    path = root / safe_study_component(sid)
    if path.resolve().parent != root.resolve():
        raise ApiRefuse("study_dir_unsafe")
    return path


@contextmanager
def journal(root: Path, sid: str) -> Iterator[sqlite3.Connection]:
    db = study_path(root, sid) / "events.db"
    if not db.is_file() or db.is_symlink():
        raise ApiRefuse("study_not_found")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def read_events(root: Path, sid: str, after: int) -> list[Mapping[str, object]]:
    """Reuse core digest verification and schema upcasting without opening a writer."""
    with journal(root, sid) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE study_id=? AND seq>? ORDER BY seq", (sid, after)
        ).fetchall()
    return [event_to_wire(stored_event_from_row(row)).model_dump(mode="json") for row in rows]


def list_studies(root: Path) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for db in root.glob("*/events.db"):
        sid = db.parent.name
        try:
            with journal(root, sid) as conn:
                final = conn.execute(
                    "SELECT * FROM events WHERE study_id=? AND type='StudyFinalized' ORDER BY seq DESC LIMIT 1", (sid,)
                ).fetchone()
                created = conn.execute(
                    "SELECT * FROM events WHERE study_id=? AND type='StudyCreated' ORDER BY seq LIMIT 1", (sid,)
                ).fetchone()
            if created is None:
                continue
            creation = stored_event_from_row(created).payload
            verdict = stored_event_from_row(final).payload.get("verdict") if final else None
        except (sqlite3.Error, ApiRefuse):
            continue  # audit-ok[except_silent_continue]: unrelated files/unsafe links are not studies
        rows.append({"study_id": sid, "request": creation.get("request"),
                     "verdict": verdict, "updated_at": db.stat().st_mtime})
    return sorted(rows, key=lambda row: cast(float, row["updated_at"]), reverse=True)


def artifact_media_type(attrs: Mapping[str, object]) -> str:
    media = attrs.get("media_type")
    if isinstance(media, str) and "/" in media:
        return media
    return "application/octet-stream"


def resolve_artifact_file(
    data_root: Path, study_id: str, artifact_id: str, events: list[Mapping[str, object]]
) -> tuple[Path, str]:
    """Only registered identities can resolve bytes; stamps refine metadata."""
    meta: dict[str, object] = {}
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        descriptor = None
        if event.get("type") == "ArtifactRegistered":
            descriptor = payload
        elif event.get("type") == "GraphNodeUpserted" and payload.get("kind") == "artifact":
            descriptor = payload.get("attrs")
        if isinstance(descriptor, Mapping) and descriptor.get("artifact_id") == artifact_id:
            meta.update({key: value for key, value in descriptor.items() if value is not None})
    if not meta:
        raise ApiRefuse("artifact_not_in_journal")
    digest = meta.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ApiRefuse("artifact_digest_format")
    study = study_path(data_root, study_id)
    cas = study / "arts" / "cas"
    path = cas / digest
    if path.resolve().parent != cas.resolve() or cas.resolve() != study.resolve() / "arts" / "cas":
        raise ApiRefuse("artifact_path_unsafe")
    if not path.is_file():
        raise ApiRefuse("artifact_bytes_missing")
    return path, artifact_media_type(meta)


def _http(exc: ApiRefuse) -> HTTPException:
    return HTTPException(status_code=404 if exc.reason == "study_not_found" else 400, detail=exc.reason)


def register_console_routes(
    app: FastAPI, *, data_root: Path, web_dir: Path,
    stream_poll_interval_s: float, stream_sleep: Callable[[float], Awaitable[None]],
) -> None:
    @app.get("/studies")
    def studies_route() -> list[Mapping[str, object]]:
        return list_studies(data_root)

    @app.get("/studies/{study_id}/artifacts/{artifact_id}")
    def artifact_route(study_id: str, artifact_id: str) -> Response:
        try:
            path, media = resolve_artifact_file(data_root, study_id, artifact_id, read_events(data_root, study_id, 0))
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != path.name:
                raise ApiRefuse("artifact_digest_mismatch")
        except ApiRefuse as exc:
            raise _http(exc) from exc
        return Response(content, media_type=media, headers={
            "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    @app.get("/console/studies/{study_id}/events")
    async def events_route(study_id: str, after: str) -> list[Mapping[str, object]]:
        try:
            return await asyncio.to_thread(read_events, data_root, study_id, parse_after_query(after))
        except ApiRefuse as exc:
            raise _http(exc) from exc

    @app.websocket("/console/studies/{study_id}/stream")
    async def stream_route(websocket: WebSocket, study_id: str) -> None:
        try:
            after = parse_after_query(websocket.query_params.get("after"))
            batch = await asyncio.to_thread(read_events, data_root, study_id, after)
        except ApiRefuse:
            await websocket.close(code=1008, reason="console_stream_query_invalid")
            return  # audit-ok[except_silent_return]: invalid query closes WS with typed protocol reason above
        await websocket.accept()

        async def send() -> None:
            nonlocal after, batch
            while True:
                for event in batch:
                    await websocket.send_json(event)
                    after = event["seq"]  # type: ignore[assignment]
                await stream_sleep(stream_poll_interval_s)
                batch = await asyncio.to_thread(read_events, data_root, study_id, after)

        async def receive() -> None:
            while True:
                if (await websocket.receive())["type"] == "websocket.disconnect":
                    return

        async with create_task_group() as group:
            group.start_soon(send)
            try:
                await receive()
            except WebSocketDisconnect:
                pass  # audit-ok[except_silent_pass]: client disconnect terminates the stream normally
            finally:
                group.cancel_scope.cancel()

    @app.get("/", include_in_schema=False)
    def root_route() -> RedirectResponse:
        return RedirectResponse("/ui/")

    # Browser module caching must follow the same revision as the console HTML.
    @app.middleware("http")
    async def static_cache(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="console")
