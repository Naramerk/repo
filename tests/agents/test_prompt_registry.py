"""Prompt registry (in-memory): typed refusals, public API only."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

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


def _reg() -> PromptRegistry:
    item = _tmpl()
    return PromptRegistry({(item.prompt_id, item.version): item})


class _NonStrVars(Mapping[str, str]):
    """Mapping[str, str] with a non-str runtime value (consistent get/items)."""

    def __init__(self) -> None:
        self._key = "goal"
        self._val: str = "x"
        object.__setattr__(self, "_val", 5)

    def __getitem__(self, key: str) -> str:
        if key != self._key:
            raise KeyError(key)
        return self._val

    def __iter__(self) -> Iterator[str]:
        yield self._key

    def __len__(self) -> int:
        return 1


class _NonTemplateMap(Mapping[tuple[str, str], PromptTemplate]):
    """Mapping with a non-PromptTemplate runtime value (consistent get/items)."""

    def __init__(self) -> None:
        self._key: tuple[str, str] = ("demo.greet", "1")
        self._val: PromptTemplate = _tmpl()
        object.__setattr__(self, "_val", object())

    def __getitem__(self, key: tuple[str, str]) -> PromptTemplate:
        if key != self._key:
            raise KeyError(key)
        return self._val

    def __iter__(self) -> Iterator[tuple[str, str]]:
        yield self._key

    def __len__(self) -> int:
        return 1


def test_happy_get() -> None:
    ref = PromptRef(prompt_id="demo.greet", version="1")
    assert _reg().get(ref).body == "Hello {{goal}}.\n"


def test_happy_render() -> None:
    ref = PromptRef(prompt_id="demo.greet", version="1")
    assert _reg().render(ref, {"goal": "X"}) == "Hello X.\n"


def test_happy_refs() -> None:
    ref = PromptRef(prompt_id="demo.greet", version="1")
    assert _reg().refs() == frozenset({ref})


def test_get_unknown_version() -> None:
    with pytest.raises(PromptNotFoundError, match="version '9'"):
        _reg().get(PromptRef(prompt_id="demo.greet", version="9"))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"prompt_id": "", "version": "1"}, "prompt_id"),
        ({"prompt_id": "x", "version": ""}, "version"),
    ],
    ids=["empty_prompt_id", "empty_version"],
)
def test_ref_refuse_empty(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(PromptConfigError, match=match):
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
    with pytest.raises(PromptRenderError, match="must be str"):
        render_template(_tmpl(), _NonStrVars())


@pytest.mark.parametrize(
    ("prompt_id", "version", "match"),
    [("", "1", "prompt_id"), ("demo.greet", "", "version")],
    ids=["empty_prompt_id", "empty_version"],
)
def test_template_refuse_empty_id_version(prompt_id: str, version: str, match: str) -> None:
    with pytest.raises(PromptConfigError, match=match):
        PromptTemplate(
            prompt_id=prompt_id,
            version=version,
            body="Hello {{goal}}.\n",
            required_variables=("goal",),
        )


@pytest.mark.parametrize(
    ("body", "required_variables", "match"),
    [
        ("Hello {{goal}}.\n", ("goal", ""), "tuple of non-empty"),
        ("Hello {{goal}}.\n", ("goal", "goal"), "duplicates"),
        ("   \n", ("goal",), "template body is empty"),
        ("no vars\n", ("goal",), "body missing placeholders"),
        ("Hello {{goal}} {{extra}}.\n", ("goal",), "undeclared placeholders"),
    ],
    ids=[
        "empty_required_item",
        "duplicate_required",
        "empty_body",
        "body_missing_placeholder",
        "undeclared_placeholder",
    ],
)
def test_template_domain_refusals(body: str, required_variables: tuple[str, ...], match: str) -> None:
    with pytest.raises(PromptConfigError, match=match):
        PromptTemplate(
            prompt_id="demo.greet",
            version="1",
            body=body,
            required_variables=required_variables,
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "prompt_id": 1,
                "version": "1",
                "body": "Hello {{goal}}.\n",
                "required_variables": ("goal",),
            },
            "prompt_id",
        ),
        (
            {
                "prompt_id": "demo.greet",
                "version": 2,
                "body": "Hello {{goal}}.\n",
                "required_variables": ("goal",),
            },
            "version",
        ),
        (
            {
                "prompt_id": "demo.greet",
                "version": "1",
                "body": 5,
                "required_variables": ("goal",),
            },
            "body",
        ),
        (
            {
                "prompt_id": "demo.greet",
                "version": "1",
                "body": "Hello {{goal}}.\n",
                "required_variables": ["goal"],
            },
            "tuple",
        ),
    ],
    ids=["non_str_prompt_id", "non_str_version", "non_str_body", "list_required"],
)
def test_template_refuse_via_model_validate(payload: dict[str, object], match: str) -> None:
    with pytest.raises(PromptConfigError, match=match):
        PromptTemplate.model_validate(payload)


def test_template_model_copy_revalidates() -> None:
    with pytest.raises(PromptConfigError, match="template body is empty"):
        _tmpl().model_copy(update={"body": ""})


def test_template_model_construct_revalidates() -> None:
    with pytest.raises(PromptConfigError, match="template body is empty"):
        PromptTemplate.model_construct(
            prompt_id="demo.greet",
            version="1",
            body="",
            required_variables=("goal",),
        )


def test_registry_refuse_empty() -> None:
    with pytest.raises(PromptConfigError, match="no templates"):
        PromptRegistry({})


def test_registry_refuse_key_mismatch() -> None:
    with pytest.raises(PromptConfigError, match="does not match"):
        PromptRegistry({("other", "1"): _tmpl()})


def test_registry_refuse_non_template() -> None:
    with pytest.raises(PromptConfigError, match="got object"):
        PromptRegistry(_NonTemplateMap())
