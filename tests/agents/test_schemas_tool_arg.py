"""ToolArg.value_json tolerant-parse ladder (parse-or-refuse, canonical text out)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from coscientist.agents.schemas import ExecutorRouteOut, ToolArg


def test_value_json_raw_control_char_recovers_lenient() -> None:
    # Raw newline inside a JSON string: strict json.loads refuses, strict=False
    # recovers the same value; canonical text re-parses to it unchanged.
    raw = '{"path": "line1\nline2"}'
    arg = ToolArg(key="data", value_json=raw)
    assert arg.value_json == '{"path":"line1\\nline2"}'
    assert json.loads(arg.value_json) == {"path": "line1\nline2"}


def test_value_json_single_quoted_dict_recovers_literal_eval() -> None:
    arg = ToolArg(key="query", value_json="{'q': 'ribosome', 'limit': 3}")
    assert arg.value_json == '{"q":"ribosome","limit":3}'
    assert json.loads(arg.value_json) == {"q": "ribosome", "limit": 3}


def test_value_json_python_dialect_recovers_literal_eval() -> None:
    arg = ToolArg(
        key="opts",
        value_json="{'flag': True, 'off': False, 'none': None, 'seq': [1, 2,],}",
    )
    assert arg.value_json == '{"flag":true,"off":false,"none":null,"seq":[1,2]}'
    assert json.loads(arg.value_json) == {
        "flag": True,
        "off": False,
        "none": None,
        "seq": [1, 2],
    }


def test_value_json_bare_prose_recovers_as_json_string() -> None:
    # Live GLM slip (st_idem_516c443e2900e2f98b734152): a bare prose sentence
    # with zero JSON structure. The delivered value IS the text written.
    raw = "Toxicological profile and biologically active compounds"
    arg = ToolArg(key="profile", value_json=raw)
    assert arg.value_json == '"Toxicological profile and biologically active compounds"'
    assert json.loads(arg.value_json) == raw


def test_value_json_prose_with_punctuation_recovers_as_json_string() -> None:
    # Colons/commas are prose punctuation, not JSON structure — still a string.
    raw = "dose: 5 mg, route: oral"
    arg = ToolArg(key="dose", value_json=raw)
    assert arg.value_json == '"dose: 5 mg, route: oral"'
    assert json.loads(arg.value_json) == raw


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": ',  # truncated JSON (structure characters present)
        "{a: b: c}",  # garbage with object structure
        "{1, 2}",  # set literal — no JSON form, structure characters present
    ],
)
def test_value_json_unrecoverable_raises_unchanged(raw: str) -> None:
    with pytest.raises(ValidationError, match="value_json must be valid JSON text"):
        ToolArg(key="x", value_json=raw)


@pytest.mark.parametrize("raw", ["", "   "])
def test_value_json_empty_or_whitespace_raises(raw: str) -> None:
    with pytest.raises(ValidationError):
        ToolArg(key="x", value_json=raw)


def test_value_json_whitespace_only_refuses_through_ladder() -> None:
    with pytest.raises(ValidationError, match="value_json must be valid JSON text"):
        ToolArg(key="x", value_json="   ")


def test_value_json_valid_json_passes_through_byte_identical() -> None:
    for raw in ('{"a": 1}', "[1, 2]", '"s"', "1", "null", ' { "spaced" : true } '):
        assert ToolArg(key="x", value_json=raw).value_json == raw


def test_value_json_dict_before_validator_still_canonicalizes() -> None:
    arg = ToolArg(key="x", value_json={"b": True, "a": [1, 2]})  # type: ignore[arg-type]
    assert arg.value_json == '{"b":true,"a":[1,2]}'


def test_executor_route_out_values_recover_normalized_form() -> None:
    # Same path the adapter uses on live model output (model_validate_json):
    # value_json decodes to text holding a raw newline (single-escaped on the
    # wire) and to a bare prose sentence — both must normalize, not refuse.
    payload = (
        '{"summary": "done", '
        '"values": ['
        '{"key": "body", "value_json": "{\\"text\\": \\"line1\\nline2\\"}"},'
        '{"key": "profile", "value_json": "Toxicological profile"}], '
        '"tool_found": ["static:search"], '
        '"expansion_request": null}'
    )
    out = ExecutorRouteOut.model_validate_json(payload)
    assert [v.value_json for v in out.values] == [
        '{"text":"line1\\nline2"}',
        '"Toxicological profile"',
    ]
