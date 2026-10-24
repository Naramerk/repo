"""Prompt registry (in-memory): typed refusals, public API only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


def _tmpl_any(
    prompt_id: Any = "demo.greet",
    version: Any = "1",
    body: Any = "Hello {{goal}}.\n",
    required_variables: Any = ("goal",),
) -> PromptTemplate:
    """Boundary factory: Any args exercise runtime type refusals."""
    return PromptTemplate(
        prompt_id=prompt_id,
        version=version,
        body=body,
        required_variables=required_variables,
    )


def _reg(tmpl: PromptTemplate | None = None) -> PromptRegistry:
    item = tmpl if tmpl is not None else _tmpl()
    return PromptRegistry({(item.prompt_id, item.version): item})


def _render_any(tmpl: PromptTemplate, variables: Any) -> str:
    return render_template(tmpl, variables)


def _registry_any(templates: Any) -> PromptRegistry:
    return PromptRegistry(templates)


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
)
def test_ref_refuse_empty(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        PromptRef(**kwargs)


@pytest.mark.parametrize(
    ("variables", "match"),
    [
        ({}, "missing"),
        ({"goal": "g", "extra": "x"}, "undeclared"),
    ],
)
def test_render_refusals_keys(
    variables: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(PromptRenderError, match=match):
        render_template(_tmpl(), variables)


def test_render_refuse_non_str_value() -> None:
    with pytest.raises(PromptRenderError, match="must be str"):
        _render_any(_tmpl(), {"goal": 1})


@pytest.mark.parametrize(
    ("prompt_id", "version", "body", "required_variables", "match"),
    [
        ("", "1", "Hello {{goal}}.\n", ("goal",), "non-empty"),
        ("demo.greet", "", "Hello {{goal}}.\n", ("goal",), "non-empty"),
        (
            "demo.greet",
            "1",
            "Hello {{goal}}.\n",
            ("goal", ""),
            "tuple of non-empty",
        ),
        (
            "demo.greet",
            "1",
            "Hello {{goal}}.\n",
            ("goal", "goal"),
            "duplicates",
        ),
        ("demo.greet", "1", "   \n", ("goal",), "template body is empty"),
        (
            "demo.greet",
            "1",
            "no vars\n",
            ("goal",),
            "body missing placeholders",
        ),
        (
            "demo.greet",
            "1",
            "Hello {{goal}} {{extra}}.\n",
            ("goal",),
            "undeclared placeholders",
        ),
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
    with pytest.raises(PromptConfigError, match="tuple of non-empty"):
        _tmpl_any(required_variables=["goal"])


def test_registry_refuse_empty() -> None:
    with pytest.raises(PromptConfigError, match="no templates"):
        PromptRegistry({})


def test_registry_refuse_key_mismatch() -> None:
    with pytest.raises(PromptConfigError, match="does not match"):
        PromptRegistry({("other", "1"): _tmpl()})


def test_registry_refuse_non_template() -> None:
    raw: Mapping[tuple[str, str], object] = {("demo.greet", "1"): object()}
    with pytest.raises(PromptConfigError, match="must be PromptTemplate"):
        _registry_any(raw)
