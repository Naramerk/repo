"""Prompt registry (in-memory): typed refusals, public API only."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from coscientist.agents.prompts import (
    PromptConfigError,
    PromptNotFoundError,
    PromptRef,
    PromptRegistry,
    PromptRenderError,
    PromptTemplate,
    render_template,
)


def _tmpl(
    *,
    prompt_id: str = "demo.greet",
    version: str = "1",
    body: str = "Hello {{goal}}.\n",
    required_variables: tuple[str, ...] = ("goal",),
) -> PromptTemplate:
    return PromptTemplate(
        prompt_id=prompt_id,
        version=version,
        body=body,
        required_variables=required_variables,
    )


def _reg(tmpl: PromptTemplate | None = None) -> PromptRegistry:
    item = tmpl if tmpl is not None else _tmpl()
    return PromptRegistry({(item.prompt_id, item.version): item})


def test_happy_get_render_refs() -> None:
    greeter = PromptRef(prompt_id="demo.greet", version="1")
    reg = _reg()
    assert reg.get(greeter).body == "Hello {{goal}}.\n"
    assert reg.render(greeter, {"goal": "X"}) == "Hello X.\n"
    assert reg.refs() == frozenset({greeter})


def test_get_unknown_version() -> None:
    ref = PromptRef(prompt_id="demo.greet", version="9")
    with pytest.raises(PromptNotFoundError, match="version '9'"):
        _reg().get(ref)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"prompt_id": "", "version": "1"}, "prompt_id"),
        ({"prompt_id": "x", "version": ""}, "version"),
    ],
    ids=["empty_prompt_id", "empty_version"],
)
def test_ref_refuse_empty(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        PromptRef(**kwargs)


@pytest.mark.parametrize(
    ("variables", "match"),
    [({}, "missing"), ({"goal": "g", "extra": "x"}, "undeclared")],
    ids=["missing_vars", "undeclared_vars"],
)
def test_render_refusals_keys(variables: dict[str, str], match: str) -> None:
    with pytest.raises(PromptRenderError, match=match):
        render_template(_tmpl(), variables)


def test_render_refuse_non_str_value() -> None:
    variables: dict[str, object] = {"goal": 1}
    with pytest.raises(PromptRenderError, match="must be str"):
        render_template(_tmpl(), variables)


@pytest.mark.parametrize(
    ("prompt_id", "version", "body", "required_variables", "match"),
    [
        ("", "1", "Hello {{goal}}.\n", ("goal",), "non-empty"),
        ("demo.greet", "", "Hello {{goal}}.\n", ("goal",), "non-empty"),
        ("demo.greet", "1", "Hello {{goal}}.\n", ("goal", ""), "tuple of non-empty"),
        ("demo.greet", "1", "Hello {{goal}}.\n", ("goal", "goal"), "duplicates"),
        ("demo.greet", "1", "   \n", ("goal",), "template body is empty"),
        ("demo.greet", "1", "no vars\n", ("goal",), "body missing placeholders"),
        (
            "demo.greet",
            "1",
            "Hello {{goal}} {{extra}}.\n",
            ("goal",),
            "undeclared placeholders",
        ),
    ],
    ids=[
        "empty_prompt_id",
        "empty_version",
        "empty_required_item",
        "duplicate_required",
        "empty_body",
        "body_missing_placeholder",
        "undeclared_placeholder",
    ],
)
def test_template_direct_refusals(
    prompt_id: str,
    version: str,
    body: str,
    required_variables: tuple[str, ...],
    match: str,
) -> None:
    with pytest.raises(PromptConfigError, match=match):
        PromptTemplate(
            prompt_id=prompt_id,
            version=version,
            body=body,
            required_variables=required_variables,
        )


def test_template_refuse_required_variables_list() -> None:
    data: dict[str, object] = {
        "prompt_id": "demo.greet",
        "version": "1",
        "body": "Hello {{goal}}.\n",
        "required_variables": ["goal"],
    }
    with pytest.raises(PromptConfigError, match="tuple of non-empty"):
        PromptTemplate(**data)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("prompt_id", 1, "prompt_id must be a non-empty string"),
        ("version", 2, "version must be a non-empty string"),
        ("body", 5, "body must be a str"),
    ],
    ids=["non_str_prompt_id", "non_str_version", "non_str_body"],
)
def test_template_refuse_non_str_fields(
    field: str,
    value: object,
    match: str,
) -> None:
    data: dict[str, object] = {
        "prompt_id": "demo.greet",
        "version": "1",
        "body": "Hello {{goal}}.\n",
        "required_variables": ("goal",),
        field: value,
    }
    with pytest.raises(PromptConfigError, match=match):
        PromptTemplate(**data)


def test_registry_refuse_empty() -> None:
    with pytest.raises(PromptConfigError, match="no templates"):
        PromptRegistry({})


def test_registry_refuse_key_mismatch() -> None:
    with pytest.raises(PromptConfigError, match="does not match"):
        PromptRegistry({("other", "1"): _tmpl()})


def test_registry_refuse_non_template() -> None:
    raw: Mapping[tuple[str, str], object] = {("demo.greet", "1"): object()}
    with pytest.raises(PromptConfigError, match="must be PromptTemplate"):
        PromptRegistry(raw)
