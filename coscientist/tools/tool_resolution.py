"""``tool_resolution`` statuses — single source: ``graph_schema.yaml`` experiment field."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Final, Literal, get_args

import yaml

# Static Literal for pydantic/openai schemas; must match graph_schema (asserted below).
ToolResolutionStatus = Literal["tool_found", "tool_built", "coder", "tool_missing"]


@lru_cache(maxsize=1)
def tool_resolution_statuses() -> tuple[str, ...]:
    """Enum values of ``experiment.tool_resolution`` from packaged graph_schema.yaml."""
    raw = resources.files("coscientist.schema").joinpath("graph_schema.yaml").read_text(
        encoding="utf-8"
    )
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise RuntimeError("graph_schema.yaml root must be a mapping")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("graph_schema.yaml nodes must be a list")
    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") != "experiment":
            continue
        fields = node.get("fields")
        if not isinstance(fields, list):
            raise RuntimeError("graph_schema experiment.fields must be a list")
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != "tool_resolution":
                continue
            typ = field.get("type")
            if type(typ) is not str or not typ.startswith("enum(") or not typ.endswith(")"):
                raise RuntimeError(
                    f"graph_schema tool_resolution.type must be enum(...), got {typ!r}"
                )
            body = typ[len("enum(") : -1]
            values = tuple(part.strip() for part in body.split("|") if part.strip())
            if not values:
                raise RuntimeError("graph_schema tool_resolution enum is empty")
            return values
        raise RuntimeError("graph_schema experiment is missing tool_resolution field")
    raise RuntimeError("graph_schema.yaml is missing experiment node")


TOOL_RESOLUTION_STATUSES: Final[tuple[str, ...]] = tool_resolution_statuses()
if frozenset(TOOL_RESOLUTION_STATUSES) != frozenset(get_args(ToolResolutionStatus)):
    raise RuntimeError(
        "ToolResolutionStatus Literal out of sync with graph_schema tool_resolution: "
        f"literal={get_args(ToolResolutionStatus)!r} schema={TOOL_RESOLUTION_STATUSES!r}"
    )
