"""Recursive strict-schema sanitizer for OpenAI structured outputs (issue #265).

OpenAI strict structured outputs apply at EVERY object level of a response
schema: every property of every object — including ``$ref``/``$defs``-reachable
ones — must be listed in ``required``, and an optional property is expressed as
required + nullable (``anyOf[…, null]``). Pydantic ``model_json_schema`` keeps
defaulted fields out of ``required``, so a nested graph (executor-route's
``ExpansionRequest`` → ``TaskSpec`` → ``OutputSpec``/``Rubric``/…) is rejected
by a strict provider at request time even when the top level is closed.

The sanitizer is applied at the single wire boundary —
``JsonSchemaResponseFormat.to_openai_payload`` — and only when ``strict`` is
true. Contract:

* every object schema with a non-empty ``properties`` mapping gets
  ``required`` = all property names (document order);
* a property that was NOT previously required is widened to
  ``anyOf[property, null]`` — the only permitted extension. Already-nullable
  properties are not re-wrapped; previously required properties keep their
  exact schema (no widening there);
* ``$ref``/``$defs`` are preserved verbatim (no inlining). Traversal walks the
  document tree only — pointer targets are never followed, so cyclic ``$ref``
  graphs terminate and stay cyclic;
* ``additionalProperties``, ``default`` and every other keyword are carried
  over unchanged: the sanitizer neither adds nor removes anything beyond the
  two rules above;
* deterministic: output key order derives from input order only;
* idempotent: ``sanitize(sanitize(s)) == sanitize(s)``.

Known consequence (the point of the slice): a body omitting an optional key is
valid under the original schema and invalid under the sanitized one — that is
the provider-mandated strict restriction; the model must emit every key and
use ``null`` for empty optionals. Bodies produced by ``model_dump`` (every key
present) are valid under both schemas. Client-side acceptance keeps validating
against the original pydantic model — the acceptance gate is unchanged.
"""

from __future__ import annotations

from typing import Mapping

_NULL: dict[str, object] = {"type": "null"}

# JSON Schema keywords whose values are schema mappings keyed by name.
_SCHEMA_MAP_KEYWORDS = ("$defs", "definitions", "patternProperties", "properties")
# Keywords whose values are lists of schemas.
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
# Keywords whose value is a single schema (or a boolean, carried over as-is).
_SCHEMA_SINGLE_KEYWORDS = (
    "additionalProperties",
    "contains",
    "else",
    "if",
    "items",
    "not",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
# Nullability-check keywords inside ``anyOf``/``oneOf`` unions.
_UNION_KEYWORDS = ("anyOf", "oneOf")


def sanitize_strict_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """Return the strict-provider form of ``schema`` (see module docstring)."""
    return _sanitize_mapping(schema, {})


def _sanitize_mapping(
    node: Mapping[str, object],
    done: dict[int, dict[str, object]],
) -> dict[str, object]:
    memo = done.get(id(node))
    if memo is not None:
        return memo
    out: dict[str, object] = {}
    done[id(node)] = out
    for key, value in node.items():
        out[key] = _sanitize_keyword(key, value, done)
    _strictify_object(out, node)
    return out


def _sanitize_keyword(
    key: str,
    value: object,
    done: dict[int, dict[str, object]],
) -> object:
    """Rebuild one schema keyword's value; non-schema keywords pass through.

    ``default``/``enum``/``const``/``examples`` and the like are data, not
    schemas — never traversed (a ``default`` dict must not be strictified).
    """
    if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, Mapping):
        return {
            name: _sanitize_mapping(sub, done) if isinstance(sub, Mapping) else sub
            for name, sub in value.items()
        }
    if key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
        return [
            _sanitize_mapping(sub, done) if isinstance(sub, Mapping) else sub
            for sub in value
        ]
    if key in _SCHEMA_SINGLE_KEYWORDS and isinstance(value, Mapping):
        return _sanitize_mapping(value, done)
    return value


def _strictify_object(out: dict[str, object], original: Mapping[str, object]) -> None:
    """All-properties ``required`` + optional→nullable on one object schema.

    ``out`` already carries sanitized ``properties``; the previously-required
    set comes from ``original``. Nodes without a non-empty ``properties``
    mapping (enums, ``$ref`` entries, non-object schemas) are left alone.
    """
    props = out.get("properties")
    if not isinstance(props, Mapping) or not props:
        return
    original_required = original.get("required")
    was_required = (
        {name for name in original_required if isinstance(name, str)}
        if isinstance(original_required, list)
        else set()
    )
    strict_props: dict[str, object] = {}
    for name, sub in props.items():
        if name not in was_required and isinstance(sub, Mapping) and not _admits_null(sub):
            strict_props[name] = {"anyOf": [sub, dict(_NULL)]}
        else:
            strict_props[name] = sub
    out["properties"] = strict_props
    out["required"] = [name for name in props] + [
        name for name in was_required if name not in props
    ]


def _admits_null(schema: object, seen: frozenset[int] = frozenset()) -> bool:
    """True when ``schema`` already admits JSON ``null`` (no wrap needed).

    Local check only: ``type`` (single or list), ``enum``/``const`` with None,
    or an ``anyOf``/``oneOf`` member admitting null. A bare ``$ref`` is NOT
    resolved — wrapping it with ``null`` is the safe (widening) direction.
    """
    if not isinstance(schema, Mapping) or id(schema) in seen:
        return False
    seen = seen | {id(schema)}
    declared = schema.get("type")
    if declared == "null":
        return True
    if isinstance(declared, list) and "null" in declared:
        return True
    if "const" in schema and schema.get("const") is None:
        return True
    values = schema.get("enum")
    if isinstance(values, list) and None in values:
        return True
    for keyword in _UNION_KEYWORDS:
        members = schema.get(keyword)
        if isinstance(members, list) and any(_admits_null(m, seen) for m in members):
            return True
    return False
