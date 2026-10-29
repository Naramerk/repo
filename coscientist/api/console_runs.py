"""Named-preset launches reuse the existing run owner and durable reservations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from coscientist.api.errors import ApiRefuse, StudyRunRefuse
from coscientist.api.idempotency import atomic_write, reserve_key, wait_complete
from coscientist.api.journal import read_events, safe_study_component, study_path
from coscientist.api.local_profiles import LocalProfiles, ProfileRefuse
from coscientist.api.presets import PinnedPreset, PresetRefuse
from coscientist.api.run import RunStudyBody, StudyRunService
from coscientist.api.run_binding import recorded_run_binding
from coscientist.api.views import EventWireDict
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict, StudyNotFound
from coscientist.core.events.ids import study_id_from_idempotency_key

log = logging.getLogger(__name__)


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    preset_id: str = Field(min_length=1)
    task_text: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    profile_id: str | None = None  # audit-ok[field_default]: absent on historical launch requests; no profile assigned


def _finalized_payload(events: list[EventWireDict]) -> Mapping[str, object] | None:
    finals = [event for event in events if event["type"] == "StudyFinalized"]
    if not finals:
        return None
    return finals[-1]["payload"]


class ConsoleRunService:
    def __init__(self, root: Path, *, runs: StudyRunService, presets: dict[str, PinnedPreset], wait_s: float) -> None:
        self._root = root
        self._runs = runs
        self._presets = presets
        self._wait_s = wait_s
        self._jobs: dict[str, asyncio.Task[dict[str, object]]] = {}
        self.profiles = LocalProfiles(root)

    def preset_metadata(self) -> list[dict[str, object]]:
        return [preset.metadata() for preset in self._presets.values()]

    async def launch(self, request: LaunchBody) -> dict[str, object]:
        if not self._presets:
            raise PresetRefuse("console_launch_not_configured")
        preset = self._presets.get(request.preset_id)
        if preset is None:
            raise PresetRefuse("preset_unknown")
        body = preset.body(request.task_text)
        if request.profile_id is not None:
            await asyncio.to_thread(self.profiles.get, request.profile_id)
        key = f"console-run:{request.idempotency_key}"
        sid = study_id_from_idempotency_key(key)
        identity: dict[str, object] = {"body": body.model_dump(mode="json"), "preset_digest": preset.digest}
        context = None
        if request.profile_id is not None:
            identity["profile_id"] = request.profile_id
            context = {"profile_id": request.profile_id}
        digest = sha256_hex(canonical_json(identity))
        path = self._root / "idempotency" / "console-runs" / sha256_hex(key)
        try:
            mapping, created = await reserve_key(
                path, study_id=sid, request_digest=digest, key=key, request_label="console launch",
                context=context,
            )
        except Conflict as exc:
            raise PresetRefuse("launch_idempotency_conflict") from exc
        if request.profile_id is not None:
            if mapping.get("context") != context:
                raise ProfileRefuse("launch_profile_context_invalid", 409)
            try:
                await asyncio.to_thread(self.profiles.bind_launch, request.profile_id, sid, digest)
            except ProfileRefuse:
                if created:
                    atomic_write(path, {**mapping, "status": "complete", "failure": "ProfileRefuse"})
                raise
        if created:
            job = asyncio.create_task(self._start(sid, body, path, mapping))
            self._jobs[sid] = job
            return await asyncio.shield(job)
        existing_job = self._jobs.get(sid)
        if existing_job is not None:
            await asyncio.shield(existing_job)
            return self.status(sid)
        if mapping.get("status") != "complete":
            try:
                mapping = await wait_complete(path, timeout_s=self._wait_s)
            except Conflict as exc:
                raise PresetRefuse("launch_owner_unavailable_no_restart") from exc
        if mapping.get("failure") is not None:
            raise PresetRefuse("launch_refused")
        return self.status(sid)

    async def _start(
        self, sid: str, body: RunStudyBody, path: Path, mapping: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            await self._runs.post_run(sid, body)
        except ApiRefuse as exc:
            log.warning("console launch refused for %s: %s", sid, exc.reason)
            atomic_write(path, {**dict(mapping), "status": "complete", "failure": type(exc).__name__})
            raise PresetRefuse("launch_refused") from exc
        atomic_write(path, {**dict(mapping), "status": "complete", "failure": None})
        return self.status(sid)

    def status(self, study_id: str) -> dict[str, object]:
        sid = safe_study_component(study_id)
        try:
            owner_status = self._runs.console_status(sid)
        except (StudyRunRefuse, StudyNotFound):
            log.debug("console owner absent; inspect recorded journal status for %s", sid)
        else:
            if owner_status["run_status"] == "running" or owner_status.get("verdict") is not None:
                return owner_status
            active = str(owner_status["active_study_id"])
            if (study_path(self._root, active) / "events.db").is_file():
                payload = _finalized_payload(read_events(self._root, active, 0))
                if payload is not None:
                    return {**owner_status, "journal_finalized": True, "verdict": payload["verdict"]}
            return {**owner_status, "journal_finalized": False, "can_cancel": False}
        binding = recorded_run_binding(self._root, sid)
        owner, active = sid, sid
        if binding is not None:
            owner, active = binding
        if not (study_path(self._root, active) / "events.db").is_file():
            return {"study_id": owner, "active_study_id": active,
                    "run_status": "owner_unavailable", "can_cancel": False}
        payload = _finalized_payload(read_events(self._root, active, 0))
        if payload is not None:
            return {"study_id": owner, "active_study_id": active, "run_status": "recorded_terminal",
                    "verdict": payload["verdict"], "can_cancel": False}
        return {"study_id": owner, "active_study_id": active, "run_status": "owner_unavailable",
                "can_cancel": False}
