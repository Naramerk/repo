"""tool_resolution Literal ↔ graph_schema contract (explicit load; match= refuses)."""

from __future__ import annotations

import pytest

from coscientist.tools.tool_resolution import (
    ToolResolutionStatus,
    assert_tool_resolution_matches_graph_schema,
    load_packaged_graph_schema_text,
    tool_resolution_canonical_type,
    tool_resolution_type_from_schema_text,
)


def test_packaged_graph_schema_matches_literal() -> None:
    assert_tool_resolution_matches_graph_schema()
    typ = tool_resolution_type_from_schema_text(load_packaged_graph_schema_text())
    assert typ == tool_resolution_canonical_type()
    assert typ == "enum(tool_found|tool_built|coder|tool_missing)"
    assert ToolResolutionStatus.__args__ == ("tool_found", "tool_built", "coder", "tool_missing")


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
def test_tool_resolution_type_from_schema_text_refuses(raw: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        tool_resolution_type_from_schema_text(raw)


def test_assert_tool_resolution_mismatch_raises() -> None:
    raw = "nodes:\n  - kind: experiment\n    fields:\n      - name: tool_resolution\n        type: enum(tool_found)\n"
    with pytest.raises(RuntimeError, match="must equal typed contract"):
        assert_tool_resolution_matches_graph_schema(raw=raw)
