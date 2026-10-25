"""``tool_resolution`` statuses — single Literal contract cross-checked vs graph_schema.

``ToolResolutionStatus`` is the single source of truth. The packaged
``graph_schema.yaml`` enum is derived-checked against it by
``assert_tool_resolution_matches_graph_schema`` (explicit call; no import-time I/O).
"""

from __future__ import annotations

from importlib import resources
from typing import Literal, get_args

import yaml

ToolResolutionStatus = Literal["tool_found", "tool_built", "coder", "tool_missing"]


def _schema_tool_resolution_type(raw: str) -> str:
    """Return ``experiment.tool_resolution`` type string from graph_schema YAML text."""
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
            if type(typ) is not str or not typ:
                raise RuntimeError(f"graph_schema tool_resolution.type must be a non-empty str, got {typ!r}")
            return typ
        raise RuntimeError("graph_schema experiment is missing tool_resolution field")
    raise RuntimeError("graph_schema.yaml is missing experiment node")


def assert_tool_resolution_matches_graph_schema(*, raw: str | None = None) -> None:
    """Fail if packaged (or provided) graph_schema tool_resolution enum ≠ Literal contract."""
    if raw is None:
        raw = resources.files("coscientist.schema").joinpath("graph_schema.yaml").read_text(encoding="utf-8")
    typ = _schema_tool_resolution_type(raw)
    expected = "enum(" + "|".join(get_args(ToolResolutionStatus)) + ")"
    if typ != expected:
        raise RuntimeError(f"graph_schema tool_resolution.type must equal typed contract {expected!r}, got {typ!r}")
