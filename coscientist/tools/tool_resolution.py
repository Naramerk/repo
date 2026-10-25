"""``tool_resolution`` statuses — single source: ``graph_schema.yaml`` experiment field.

The typed contract is ``ToolResolutionStatus`` (Literal). ``graph_schema.yaml`` must
declare the same enum body verbatim; there is no pipe-split / heuristic parse.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Final, Literal, get_args

import yaml

# Typed contract (pydantic / OpenAI schemas). Must match graph_schema experiment field.
ToolResolutionStatus = Literal["tool_found", "tool_built", "coder", "tool_missing"]

_STATUS_VALUES: Final[tuple[str, ...]] = get_args(ToolResolutionStatus)
_CANONICAL_TYPE: Final[str] = "enum(" + "|".join(_STATUS_VALUES) + ")"


@lru_cache(maxsize=1)
def tool_resolution_statuses() -> tuple[str, ...]:
    """Return ``ToolResolutionStatus`` values after verifying graph_schema matches them."""
    raw = resources.files("coscientist.schema").joinpath("graph_schema.yaml").read_text(encoding="utf-8")
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
            if typ != _CANONICAL_TYPE:
                raise RuntimeError(
                    f"graph_schema tool_resolution.type must equal typed contract {_CANONICAL_TYPE!r}, got {typ!r}"
                )
            return _STATUS_VALUES
        raise RuntimeError("graph_schema experiment is missing tool_resolution field")
    raise RuntimeError("graph_schema.yaml is missing experiment node")


TOOL_RESOLUTION_STATUSES: Final[tuple[str, ...]] = tool_resolution_statuses()
if TOOL_RESOLUTION_STATUSES != _STATUS_VALUES:
    raise RuntimeError(
        "TOOL_RESOLUTION_STATUSES out of sync with ToolResolutionStatus: "
        f"loaded={TOOL_RESOLUTION_STATUSES!r} literal={_STATUS_VALUES!r}"
    )
