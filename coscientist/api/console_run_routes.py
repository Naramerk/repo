"""Small human launch contract; advanced settings remain on the server."""

from fastapi import FastAPI, HTTPException

from coscientist.api.console_runs import ConsoleRunService, LaunchBody
from coscientist.api.errors import ApiRefuse


def register_console_run_routes(app: FastAPI, *, service: ConsoleRunService, poll_interval_s: float) -> None:
    @app.get("/console/run-presets")
    def presets() -> dict[str, object]:
        return {"presets": service.preset_metadata(), "poll_interval_ms": poll_interval_s * 1000}

    @app.post("/console/runs")
    async def launch(body: LaunchBody) -> dict[str, object]:
        try:
            return await service.launch(body)
        except ApiRefuse as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc

    @app.get("/console/runs/{study_id}")
    def status(study_id: str) -> dict[str, object]:
        try:
            return service.status(study_id)
        except ApiRefuse as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
