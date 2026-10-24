"""ToolResolved wire payload (DESIGN §6.3 / CAPABILITIES #8). Validated on replay."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MatchKind = Literal["exact", "none"]


class MatchInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: MatchKind
    ranked: list[str]


class ToolResolvedPayload(BaseModel):
    """Registered in ``PAYLOAD_MODELS``; replay via ``validate_event_payload`` (raw unchanged)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    status: Literal["tool_found", "tool_missing"]
    tool: str | None
    catalog_version: StrictInt = Field(ge=0)
    match: MatchInfo

    @model_validator(mode="after")
    def _status_match_tool(self) -> ToolResolvedPayload:
        found = self.status == "tool_found"
        if found != (self.tool is not None):
            raise ValueError(
                "tool_found requires tool" if found else "tool_missing requires tool=None"
            )
        want = "exact" if found else "none"
        if self.match.kind != want:
            raise ValueError(f"{self.status} requires match.kind={want}")
        if found and self.match.ranked != [self.tool]:
            raise ValueError("tool_found requires ranked=[tool]")
        return self
