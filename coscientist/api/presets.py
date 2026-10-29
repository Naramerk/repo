"""Server-owned run settings; the browser receives only deliberate metadata."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.api.errors import ApiRefuse
from coscientist.api.run import RunStudyBody, RunStudySettings
from coscientist.core.events.digest import canonical_json, sha256_hex

_FILE_FIELDS = (
    "catalog_path", "llm_config_path", "tools_json_path", "mcp_tool_bundle_path",
    "tool_call_args_path", "coder_jobs_path",
)


class PresetRefuse(ApiRefuse):
    """Unknown/unavailable server preset; never include file paths or secrets."""


class RunPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    config: RunStudySettings

    @model_validator(mode="after")
    def _interactive_only(self) -> RunPreset:
        if self.config.hitl_mode != "interactive":
            raise ValueError("console presets require interactive HITL")
        return self


class PresetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    presets: list[RunPreset]

    @model_validator(mode="after")
    def _unique(self) -> PresetDocument:
        if len({preset.id for preset in self.presets}) != len(self.presets):
            raise ValueError("preset IDs must be unique")
        return self


def _reference_hashes(settings: RunStudySettings) -> dict[str, str]:
    dumped = settings.model_dump()
    refs = [dumped[field] for field in _FILE_FIELDS]
    refs.extend(settings.task_input_paths)
    hashes: dict[str, str] = {}
    for raw in refs:
        if raw is None:
            continue
        try:
            with Path(raw).open("rb") as source:
                hashes[raw] = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            raise PresetRefuse("preset_reference_unavailable") from exc
    return hashes


@dataclass(frozen=True)
class PinnedPreset:
    preset: RunPreset
    reference_hashes: dict[str, str]
    digest: str

    def body(self, task_text: str) -> RunStudyBody:
        if _reference_hashes(self.preset.config) != self.reference_hashes:
            raise PresetRefuse("preset_configuration_changed_restart_required")
        return RunStudyBody.model_validate({**self.preset.config.model_dump(), "task_text": task_text})

    def metadata(self) -> dict[str, object]:
        settings = self.preset.config
        return {
            "id": self.preset.id, "label": self.preset.label, "description": self.preset.description,
            "profile": settings.profile,
            "models": {"planner": settings.planner_model, "executor": settings.executor_model,
                       "specification": settings.tz_spec_model},
            "deadline_s": settings.deadline_s, "max_llm_calls": settings.max_llm_calls,
            "max_tokens": settings.max_tokens, "max_tool_calls": settings.max_tool_calls,
            "max_ticks": settings.max_ticks,
        }


def load_run_presets(path: Path | None) -> dict[str, PinnedPreset]:
    if path is None:
        return {}
    document = PresetDocument.model_validate_json(path.read_text(encoding="utf-8"))
    pinned: dict[str, PinnedPreset] = {}
    for preset in document.presets:
        fields = preset.config.model_dump()
        for field in _FILE_FIELDS:
            if fields[field] is not None:
                fields[field] = str((path.parent / fields[field]).resolve())
        fields["task_input_paths"] = [str((path.parent / raw).resolve()) for raw in fields["task_input_paths"]]
        config = RunStudySettings.model_validate(fields)
        normalized = preset.model_copy(update={"config": config})
        hashes = _reference_hashes(config)
        digest = sha256_hex(canonical_json({"preset": normalized.model_dump(mode="json"), "references": hashes}))
        pinned[preset.id] = PinnedPreset(normalized, hashes, digest)
    return pinned
