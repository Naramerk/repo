"""Synthetic result projections: bounds, pointer fidelity and source contracts."""

import json
from dataclasses import replace

import pytest

from coscientist.core.execution.fact_projection import DEFAULT_LIMITS, project_facts


def _raw(value):
    return json.dumps(value).encode()


def _mcp(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": False}


def test_large_structured_payload_projects_required_result_before_bulk_rows():
    payload = {"rows": ["row-" + str(i) for i in range(3000)], "measurement": {"oxygen_mg_l": 8.125}}
    document = {"result": _mcp(payload), "content_digest": "a" * 64}
    raw = _raw(document)
    assert len(raw) > 10_240
    pointer = "/result/structuredContent/measurement/oxygen_mg_l"
    projection = project_facts(raw, required_pointers=(pointer,))
    assert projection.facts[0].pointer == pointer
    assert projection.facts[0].value_json == "8.125"
    assert projection.coverage == "partial" and projection.omitted_leaves > 0
    assert projection.unrepresented_required == ()
    assert all("/content/" not in f.pointer for f in projection.facts)
    assert len(projection.facts) == DEFAULT_LIMITS.max_facts


def test_nested_protocol_envelopes_select_structured_data_once():
    raw = _raw({"result": _mcp(_mcp({"response": 12.75})), "content_digest": "a" * 64})
    projection = project_facts(raw)
    assert projection.source_pointer == "/result/structuredContent/structuredContent"
    assert [(f.pointer, f.value_json) for f in projection.facts] == [(projection.source_pointer + "/response", "12.75")]
    assert projection.coverage == "complete"


def test_application_content_key_is_not_a_protocol_hint():
    projection = project_facts(_raw({"content": [], "oxygen": 8.125}))
    assert projection.source_pointer == ""
    assert [(f.pointer, f.value_json) for f in projection.facts] == [("/oxygen", "8.125")]


def test_application_result_and_digest_keys_do_not_hide_other_data():
    result = project_facts(_raw({"result": {"nested": 2}, "content_digest": "source", "unrelated": 23}))
    assert result.source_pointer == ""
    assert any(f.pointer == "/unrelated" and f.value_json == "23" for f in result.facts)


def test_integer_tokens_do_not_round_or_lose_signed_zero():
    result = project_facts(b'{"large":9007199254740993,"zero":-0}')
    assert [f.value_json for f in result.facts] == ["9007199254740993", "-0"]


def test_required_scalars_precede_required_bulk_containers():
    projection = project_facts(
        _raw({"bulk": list(range(500)), "endpoint": 97.625}), required_pointers=("/bulk", "/endpoint")
    )
    assert projection.facts[0].pointer == "/endpoint"
    assert "/bulk" in projection.unrepresented_required
    assert "/endpoint" not in projection.unrepresented_required


def test_exact_decimal_tokens_and_rfc6901_addresses_are_preserved():
    raw = b'{"a/b":{"~metric":[1.2300e-5]}}'
    pointer = "/a~1b/~0metric/0"
    projection = project_facts(raw, required_pointers=(pointer,))
    assert projection.facts[0].pointer == pointer
    assert projection.facts[0].value_json == "1.2300e-5"
    assert projection.coverage == "complete"


@pytest.mark.parametrize("pointer", ["/absent", "bad", "/~2bad", "/values/-1", "/values/01"])
def test_missing_or_invalid_selector_is_explicitly_incomplete(pointer):
    result = project_facts(_raw({"values": [23]}), required_pointers=(pointer,))
    assert result.coverage == "partial"
    assert result.unrepresented_required == (pointer,)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e9999}',
        b'{"x":1,"x":2}',
        b'{"x":',
        b"\xff",
        b'log banner\n{"x":12}',
        b'{"x":"\\ud800"}',
    ],
)
def test_invalid_sources_never_produce_facts(raw):
    result = project_facts(raw)
    assert result.coverage == "reference"
    assert result.facts == ()


@pytest.mark.parametrize(
    "limits,document,reason",
    [
        (replace(DEFAULT_LIMITS, max_bytes=10), {"x": "large"}, "byte_budget"),
        (replace(DEFAULT_LIMITS, max_nodes=2), {"x": [1, 2, 3]}, "node_budget"),
        (replace(DEFAULT_LIMITS, max_depth=2), {"x": {"y": {"z": 1}}}, "depth_budget"),
    ],
)
def test_parse_safety_budgets_are_independent_of_fact_display(limits, document, reason):
    result = project_facts(_raw(document), limits=limits)
    assert result.coverage == "reference" and reason in result.reason


def test_display_limit_omits_long_value_instead_of_inventing_a_clipped_fact():
    result = project_facts(
        _raw({"description": "very long " * 1000, "temperature": 28.5}), required_pointers=("/description",)
    )
    assert result.coverage == "partial" and result.unrepresented_required == ("/description",)
    assert [f.pointer for f in result.facts] == ["/temperature"]
    assert result.facts[0].value_json == "28.5"


def test_selector_budget_is_fail_closed():
    result = project_facts(
        b'{"x":1}', required_pointers=tuple(f"/x/{i}" for i in range(DEFAULT_LIMITS.max_selectors + 1))
    )
    assert result.coverage == "reference" and result.reason == "selector_budget"


def test_long_shared_key_prefix_refuses_before_building_many_leaf_paths():
    result = project_facts(_raw({"long" * 1000: list(range(1000))}))
    assert result.coverage == "reference" and "pointer_budget" in result.reason


def test_mcp_error_and_text_only_result_remain_reference_only():
    for source in [
        {"content": [], "isError": True, "structuredContent": {"value": 999}},
        {"content": [{"type": "text", "text": '{"value":999}'}]},
    ]:
        result = project_facts(_raw(source))
        assert result.coverage == "reference" and result.facts == ()
