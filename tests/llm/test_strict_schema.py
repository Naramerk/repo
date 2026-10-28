"""Recursive strict-schema sanitizer at the openai_compat boundary (issue #265).

OpenAI strict structured outputs reject a response_format unless EVERY object
level lists every property in ``required`` (optionals as required + nullable).
Golden tests here pin the real role schemas (executor-route 9/13 gapped
``$defs``, remote_coder_task already strict-valid, planner context schema),
round-trip against ``jsonschema`` on real ``model_dump`` bodies, reflexivity,
and adversarial shapes (cyclic ``$ref``, deep nesting, tuples).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import jsonschema
import pytest
from pydantic import BaseModel, ConfigDict

from coscientist.agents.schemas import (
    ExecutorRouteOut,
    PlanIntent,
    QuestionIntent,
    RemoteCoderTaskSpec,
    ToolAction,
    plan_intent_model_for,
)
from coscientist.core.execution.domain import (
    ExpansionRequest,
    Need,
    RequirementRef,
    TaskSpec,
)
from coscientist.llm.strict_schema import sanitize_strict_schema
from coscientist.llm.types import ChatMessage, JsonSchemaResponseFormat, StructuredLlmRequest

# Previously-gapped $defs of ExecutorRouteOut (docs/plan/adaptive-plan-runtime.md
# «Review record», issue #265): 9 of 13 had properties outside required.
_GAPPED_DEFS = (
    "ExpansionRequest",
    "ForEachSpec",
    "InputSpec",
    "OutputSpec",
    "QuestionSpec",
    "RequirementRef",
    "Rubric",
    "RubricItem",
    "TaskSpec",
)


def _object_nodes(node: object, *, _path: str = "$") -> list[tuple[str, Mapping[str, Any]]]:
    """Every schema node carrying a non-empty ``properties`` mapping, with path."""
    if not isinstance(node, Mapping):
        return []
    found: list[tuple[str, Mapping[str, Any]]] = []
    props = node.get("properties")
    if isinstance(props, Mapping) and props:
        found.append((_path, node))
    for key in ("$defs", "properties"):
        subs = node.get(key)
        if isinstance(subs, Mapping):
            for name, sub in subs.items():
                found.extend(_object_nodes(sub, _path=f"{_path}/{key}/{name}"))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        subs = node.get(key)
        if isinstance(subs, list):
            for index, sub in enumerate(subs):
                found.extend(_object_nodes(sub, _path=f"{_path}/{key}[{index}]"))
    for key in ("additionalProperties", "contains", "else", "if", "items", "not", "then"):
        sub = node.get(key)
        if isinstance(sub, Mapping):
            found.extend(_object_nodes(sub, _path=f"{_path}/{key}"))
    return found


def _strict_violations(schema: Mapping[str, Any]) -> list[str]:
    """Paths of object nodes whose ``required`` is not exactly all properties."""
    return [
        path
        for path, node in _object_nodes(schema)
        if set(node.get("required", ())) != set(node["properties"])
    ]


def _admits_null(schema: object) -> bool:
    """Test-side nullability check (independent of the sanitizer's)."""
    if not isinstance(schema, Mapping):
        return False
    declared = schema.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    values = schema.get("enum")
    if isinstance(values, list) and None in values:
        return True
    for keyword in ("anyOf", "oneOf"):
        members = schema.get(keyword)
        if isinstance(members, list) and any(_admits_null(member) for member in members):
            return True
    return False


def _ref_count(schema: object) -> int:
    if isinstance(schema, Mapping):
        return (1 if "$ref" in schema else 0) + sum(_ref_count(value) for value in schema.values())
    if isinstance(schema, list):
        return sum(_ref_count(value) for value in schema)
    return 0


def _is_null_wrap(sanitized: object, original: object) -> bool:
    """True when ``sanitized`` is exactly the optional→required+nullable wrap."""
    if not isinstance(sanitized, Mapping) or set(sanitized) != {"anyOf"}:
        return False
    members = sanitized["anyOf"]
    return (
        isinstance(members, list)
        and len(members) == 2
        and members[0] == original
        and members[1] == {"type": "null"}
    )


# --- golden: real role schemas -------------------------------------------------


def test_executor_route_out_sanitized_all_defs_required() -> None:
    """All 13 executor-route $defs become all-required (issue #265: 9 were not)."""
    original = ExecutorRouteOut.model_json_schema()
    defs = original["$defs"]
    assert len(defs) == 13
    gapped_before = {
        name
        for name, node in defs.items()
        if set(node.get("required", ())) != set(node.get("properties", ()))
    }
    assert gapped_before == set(_GAPPED_DEFS)

    sanitized = sanitize_strict_schema(original)
    assert _strict_violations(sanitized) == []
    for name, node in sanitized["$defs"].items():
        if node.get("properties"):
            assert set(node["required"]) == set(node["properties"]), name
    assert set(sanitized["required"]) == set(sanitized["properties"])


def test_sanitizer_widens_only_optionals_and_preserves_the_rest() -> None:
    """Optional props admit null; $refs, additionalProperties, defaults survive."""
    original = ExecutorRouteOut.model_json_schema()
    sanitized = sanitize_strict_schema(original)
    orig_nodes = dict(_object_nodes(original))
    san_nodes = dict(_object_nodes(sanitized))
    assert set(orig_nodes) == set(san_nodes)
    for path, orig in orig_nodes.items():
        san = san_nodes[path]
        was_required = set(orig.get("required", ()))
        assert set(orig["properties"]) == set(san["properties"]), path
        for name, prop in orig["properties"].items():
            san_prop = san["properties"][name]
            if name in was_required:
                # Previously required: never turned into the null wrap.
                assert not _is_null_wrap(san_prop, prop), (path, name)
            else:
                assert _admits_null(san_prop), (path, name)
    # No inlining: same number of $ref entries before and after.
    assert _ref_count(sanitized) == _ref_count(original)
    # additionalProperties values unchanged at every node.
    for path, orig in orig_nodes.items():
        assert orig.get("additionalProperties") == san_nodes[path].get("additionalProperties"), path
    # Defaults are not stripped (OpenAI strict ignores them; their presence is
    # fine) — a wrapped property keeps its default inside the original member.
    input_spec = sanitized["$defs"]["InputSpec"]["properties"]
    assert input_spec["kind"]["anyOf"][0]["default"] == "artifact"
    rubric = sanitized["$defs"]["Rubric"]["properties"]
    assert rubric["k"]["default"] is None  # already nullable → not wrapped


def test_remote_coder_task_schema_stays_strict_valid_positive_pin() -> None:
    """remote_coder_task is strict-valid 0/1 today; the sanitizer must not regress it.

    Positive pin (issue #265): an already-strict schema passes through with the
    sanitizer making NO change at all. The recursive all-required pin below
    catches any genuine degradation if an optional field is ever introduced.
    """
    original = RemoteCoderTaskSpec.model_json_schema()
    sanitized = sanitize_strict_schema(original)
    assert sanitized == original
    assert _strict_violations(sanitized) == []
    output_spec = sanitized["$defs"]["RemoteCoderOutputSpec"]
    assert set(output_spec["required"]) == set(output_spec["properties"])


def test_plan_intent_context_schema_preserved() -> None:
    """Planner context-typed schema: criteria/tools_hint enums survive strictifying."""
    model = plan_intent_model_for(
        tool_ids=("mcp_b", "mcp_a"),
        criterion_ids=("crit_1", "crit_2"),
    )
    sanitized = sanitize_strict_schema(model.model_json_schema())
    assert _strict_violations(sanitized) == []
    task_def = next(
        node for node in sanitized["$defs"].values() if "tools_hint" in node.get("properties", {})
    )
    # tools_hint is optional → required + nullable; the enum survives in the member.
    hint = task_def["properties"]["tools_hint"]
    assert hint["anyOf"][1] == {"type": "null"}
    assert hint["anyOf"][0]["items"]["enum"] == ["mcp_a", "mcp_b"]
    criteria_def = next(
        node for node in sanitized["$defs"].values() if "criteria" in node.get("properties", {})
    )
    criteria = criteria_def["properties"]["criteria"]
    assert criteria["anyOf"][0]["items"]["enum"] == ["crit_1", "crit_2"]
    assert set(task_def["required"]) == set(task_def["properties"])


def test_tool_action_sanitized_all_required() -> None:
    """The automatic-tools response_format is covered by the same boundary."""
    sanitized = sanitize_strict_schema(ToolAction.model_json_schema())
    assert _strict_violations(sanitized) == []


# --- round-trip: model_dump bodies valid under both schemas ---------------------


def _expansion_request() -> ExpansionRequest:
    return ExpansionRequest(
        tasks=[
            TaskSpec(
                task_key="EXP-1",
                title="follow-up",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
            )
        ],
        reason="need follow-up",
    )


def _round_trip_cases() -> list[tuple[str, type[BaseModel], BaseModel]]:
    ordinary = ExecutorRouteOut(summary="ok", values=(), tool_found=(), expansion_request=None)
    expanding = ExecutorRouteOut(
        summary="ok",
        values=(),
        tool_found=(),
        expansion_request=_expansion_request(),
    )
    coder = RemoteCoderTaskSpec(
        task="напиши и запусти скрипт",
        outputs=({"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},),
    )
    intent = PlanIntent(
        tasks=(),
        questions=(QuestionIntent(text="why", final_type="verdict"),),
        hypotheses=Need(kind="need", needed=False, reason="not needed"),
        literature=Need(kind="need", needed=True, reason="background"),
    )
    action = ToolAction(action="finish", call=None, output_json="{}", selections=())
    return [
        ("executor_route_ordinary", ExecutorRouteOut, ordinary),
        ("executor_route_expanding", ExecutorRouteOut, expanding),
        ("remote_coder_task", RemoteCoderTaskSpec, coder),
        ("plan_intent", PlanIntent, intent),
        ("tool_action", ToolAction, action),
    ]


@pytest.mark.parametrize(
    ("label", "model", "instance"),
    _round_trip_cases(),
    ids=[case[0] for case in _round_trip_cases()],
)
def test_model_dump_body_valid_under_original_and_sanitized(
    label: str,
    model: type[BaseModel],
    instance: BaseModel,
) -> None:
    """A real model_dump body validates under BOTH schemas (acceptance unchanged)."""
    del label
    original = model.model_json_schema()
    sanitized = sanitize_strict_schema(original)
    body = instance.model_dump(mode="json")
    jsonschema.validate(body, original)
    jsonschema.validate(body, sanitized)


# --- reflexivity / determinism ---------------------------------------------------


def test_sanitize_is_idempotent_and_deterministic() -> None:
    schemas: list[dict[str, object]] = [
        model.model_json_schema() for model in (ExecutorRouteOut, RemoteCoderTaskSpec, PlanIntent, ToolAction)
    ]
    schemas.append(_cyclic_ref_schema())
    schemas.append(_deep_nesting_schema(40))
    for schema in schemas:
        once = sanitize_strict_schema(schema)
        assert sanitize_strict_schema(once) == once
        assert json.dumps(sanitize_strict_schema(schema), ensure_ascii=False) == json.dumps(
            once, ensure_ascii=False
        )


# --- adversarial ------------------------------------------------------------------


def _cyclic_ref_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}},
        "required": ["a"],
        "$defs": {
            "A": {
                "type": "object",
                "properties": {"self": {"$ref": "#/$defs/A"}, "opt": {"type": "string"}},
                "required": [],
            }
        },
    }


def test_cyclic_ref_stays_ref_without_inlining() -> None:
    """Pointer cycles terminate and $defs keep pointing — never unrolled."""
    sanitized = sanitize_strict_schema(_cyclic_ref_schema())
    defs = sanitized["$defs"]
    assert set(defs) == {"A"}
    entry = defs["A"]
    assert set(entry["required"]) == {"self", "opt"}
    # The cyclic property still points at #/$defs/A, now nullable.
    assert entry["properties"]["self"] == {"anyOf": [{"$ref": "#/$defs/A"}, {"type": "null"}]}
    assert _ref_count(sanitized) == 2  # root property ref + wrapped cycle ref


def _deep_nesting_schema(depth: int) -> dict[str, object]:
    node: dict[str, object] = {"type": "object", "properties": {}, "required": []}
    for _ in range(depth):
        node = {"type": "object", "properties": {"n": node}, "required": []}
    return node


def test_deep_nesting_all_levels_required() -> None:
    sanitized = sanitize_strict_schema(_deep_nesting_schema(60))
    assert _strict_violations(sanitized) == []
    level = sanitized
    for _ in range(60):
        assert level["required"] == ["n"]
        prop = level["properties"]["n"]
        assert prop["anyOf"][1] == {"type": "null"}
        level = prop["anyOf"][0]
    # The innermost empty-properties leaf is untouched.
    assert level == {"type": "object", "properties": {}, "required": []}


def test_tuple_prefix_items_sanitized() -> None:
    class Grid(BaseModel):
        model_config = ConfigDict(extra="forbid")

        pair: tuple[int, str]
        maybe_cells: tuple[int, ...] = ()

    original = Grid.model_json_schema()
    sanitized = sanitize_strict_schema(original)
    assert _strict_violations(sanitized) == []
    assert set(sanitized["required"]) == {"pair", "maybe_cells"}
    assert sanitized["properties"]["pair"]["prefixItems"][0]["type"] == "integer"
    assert _admits_null(sanitized["properties"]["maybe_cells"])
    body: dict[str, object] = {"pair": [1, "x"], "maybe_cells": []}
    jsonschema.validate(body, original)
    jsonschema.validate(body, sanitized)


def test_default_dict_value_is_not_traversed() -> None:
    """``default`` is data, not a schema: its nested object is never strictified."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "x": {"type": "object", "default": {"a": 1}},
            "y": {"type": "string"},
        },
        "required": ["y"],
    }
    sanitized = sanitize_strict_schema(schema)
    prop_x = sanitized["properties"]["x"]
    assert isinstance(prop_x, Mapping)
    member = prop_x["anyOf"][0]
    assert member["default"] == {"a": 1}
    assert "required" not in member["default"]
    assert member["type"] == "object"
    assert set(sanitized["required"]) == {"x", "y"}


def test_boolean_schema_members_pass_through() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"any": True, "never": False, "s": {"type": "string"}},
        "required": ["s"],
    }
    sanitized = sanitize_strict_schema(schema)
    props = sanitized["properties"]
    assert isinstance(props, Mapping)
    assert props["any"] is True
    assert props["never"] is False
    assert set(sanitized["required"]) == {"any", "never", "s"}


# --- wire boundary ------------------------------------------------------------------


_OPTIONAL_BEARING: dict[str, object] = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "opt": {"type": "integer", "default": 1}},
    "required": ["a"],
    "additionalProperties": False,
}


def test_to_openai_payload_sanitizes_only_when_strict() -> None:
    strict = JsonSchemaResponseFormat(name="role", strict=True, schema=dict(_OPTIONAL_BEARING))
    payload = strict.to_openai_payload()
    wire = payload["json_schema"]
    assert isinstance(wire, Mapping)
    schema = wire["schema"]
    assert isinstance(schema, Mapping)
    assert set(schema["required"]) == {"a", "opt"}
    assert _admits_null(schema["properties"]["opt"])

    lenient = JsonSchemaResponseFormat(name="role", strict=False, schema=dict(_OPTIONAL_BEARING))
    assert lenient.to_openai_payload() == {
        "type": "json_schema",
        "json_schema": {"name": "role", "strict": False, "schema": dict(_OPTIONAL_BEARING)},
    }


def test_to_openai_payload_does_not_mutate_the_format() -> None:
    """The raw ``schema_`` (system prompts, client acceptance) stays untouched."""
    snapshot = ExecutorRouteOut.model_json_schema()
    fmt = JsonSchemaResponseFormat(name="executor_route", strict=True, schema=snapshot)
    raw_before = json.loads(json.dumps(fmt.schema_, ensure_ascii=False))
    fmt.to_openai_payload()
    assert fmt.schema_ == raw_before  # sanitizing builds a new wire dict, never mutates
    assert "required" not in fmt.schema_["$defs"]["Rubric"]  # prompts embed the RAW schema
    # A structured request carries the format as-is; the wire shape is derived
    # in openai_compat._encode_request via to_openai_payload.
    request = StructuredLlmRequest(
        model="m",
        messages=[ChatMessage(role="user", content="x")],
        response_format=fmt,
    )
    assert request.response_format.strict is True
