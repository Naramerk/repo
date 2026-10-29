"""Journal HITL views and command ingress (no runtime/writer opened here)."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from coscientist.api.console import read_events
from coscientist.api.errors import ApiRefuse
from coscientist.api.hitl import AnswerBody, HitlRefuse
from coscientist.api.run import StudyRunService
from coscientist.api.views import EventWireDict


def hitl_rows(events: list[EventWireDict]) -> list[dict[str, object]]:
    rows: dict[object, dict[str, object]] = {}
    finalized = False
    for event in events:
        kind, payload = event["type"], event["payload"]
        rid = payload.get("request_id")
        if kind == "HitlOpened":
            rows[rid] = {**payload, "opened_seq": event["seq"], "status": "pending"}
        elif kind in ("HitlDecided", "HitlTimedOut") and rid in rows:
            if not payload.get("keep_open") and payload.get("effect") != "WAIT":
                rows[rid]["status"] = "answered" if kind == "HitlDecided" else "timed_out"
            rows[rid]["answer"] = payload
        elif kind == "HitlDeadlineExtended" and rid in rows:
            rows[rid]["deadline"] = payload["deadline"]
        elif kind == "StudyFinalized":
            finalized = True
    if finalized:
        for row in rows.values():
            if row["status"] == "pending":
                row["status"] = "study_closed"
    return list(rows.values())


def register_hitl_routes(app: FastAPI, *, data_root: Path, runs: StudyRunService) -> None:
    @app.get("/console/studies/{study_id}/hitl")
    def pending(study_id: str) -> list[dict[str, object]]:
        try:
            rows = hitl_rows(read_events(data_root, study_id, 0))
        except ApiRefuse as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
        for row in rows:
            row["answer_study_id"] = runs.hitl_answer_target(study_id, row["request_id"])
            row["can_answer"] = row["status"] == "pending" and row["answer_study_id"] is not None
        return rows

    @app.post("/studies/{study_id}/hitl/{request_id}")
    async def answer(study_id: str, request_id: str, body: AnswerBody) -> dict[str, object]:
        try:
            return await runs.answer_hitl(study_id, request_id, body)
        except HitlRefuse as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc
        except ApiRefuse as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
