"""Small human launch contract; advanced settings remain on the server."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from coscientist.api.console import list_studies
from coscientist.api.console_runs import ConsoleRunService, LaunchBody
from coscientist.api.errors import ApiRefuse
from coscientist.api.local_profiles import ProfileRefuse


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nickname: str = Field(min_length=1, max_length=256)


class AttachBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    study_id: str = Field(min_length=1, max_length=256)


def register_console_run_routes(app: FastAPI, *, service: ConsoleRunService, poll_interval_s: float) -> None:
    @app.get("/console/profiles")
    def profiles() -> dict[str, object]:
        try:
            return {"profiles": service.profiles.list_profiles()}
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    @app.post("/console/profiles", status_code=201)
    def create_profile(body: ProfileBody) -> dict[str, str]:
        try:
            return service.profiles.create(body.nickname)
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    @app.get("/console/profiles/{profile_id}/studies")
    def profile_studies(profile_id: str) -> list[dict[str, object]]:
        try:
            return service.profiles.study_rows(profile_id, list_studies(service.profiles.root))
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    @app.post("/console/profiles/{profile_id}/studies")
    def attach_study(profile_id: str, body: AttachBody) -> dict[str, str]:
        try:
            return service.profiles.attach(profile_id, body.study_id)
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc
        except ApiRefuse as exc:
            status = 404 if exc.reason == "study_not_found" else 400
            raise HTTPException(status_code=status, detail=exc.reason) from exc

    @app.get("/console/run-presets")
    def presets() -> dict[str, object]:
        return {"presets": service.preset_metadata(), "poll_interval_ms": poll_interval_s * 1000}

    @app.post("/console/runs")
    async def launch(body: LaunchBody) -> dict[str, object]:
        try:
            return await service.launch(body)
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc
        except ApiRefuse as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc

    @app.get("/console/runs/{study_id}")
    def status(study_id: str) -> dict[str, object]:
        try:
            return service.status(study_id)
        except ApiRefuse as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
