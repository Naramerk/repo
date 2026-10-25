"""tool_resolution Literal ↔ graph_schema contract (explicit load; mismatch= refuses)."""

from __future__ import annotations

from typing import get_args

import pytest

from coscientist.tools.tool_resolution import (
    ToolResolutionStatus,
    _schema_tool_resolution_type,
    assert_tool_resolution_matches_graph_schema,
)


def test_packaged_graph_schema_matches_literal() -> None:
    assert_tool_resolution_matches_graph_schema()
    assert list(get_args(ToolResolutionStatus)) == ["tool_found", "tool_built", "coder", "tool_missing"]


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("[]", "root must be a mapping"),
        ("nodes: {}", "nodes must be a list"),
        (
            "nodes:\n  - kind: experiment\n    fields: {}\n",
            "experiment.fields must be a list",
        ),
        (
            "nodes:\n  - kind: experiment\n    fields:\n      - name: tool_resolution\n        type: 1\n",
            "non-empty str",
        ),
        (
            "nodes:\n  - kind: experiment\n    fields:\n      - name: other\n        type: str\n",
            "missing tool_resolution field",
        ),
        ("nodes:\n  - kind: study\n    fields: []\n", "missing experiment node"),
    ],
    ids=[
        "root-not-mapping",
        "nodes-not-list",
        "fields-not-list",
        "type-not-str",
        "missing-field",
        "missing-experiment",
    ],
)
def test_schema_tool_resolution_type_refuses(raw: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        _schema_tool_resolution_type(raw)


def test_assert_tool_resolution_mismatch_raises() -> None:
    raw = "nodes:\n  - kind: experiment\n    fields:\n      - name: tool_resolution\n        type: enum(tool_found)\n"
    with pytest.raises(RuntimeError, match="must equal typed contract"):
        assert_tool_resolution_matches_graph_schema(raw=raw)
