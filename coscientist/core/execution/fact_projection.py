"""Bounded, source-addressed JSON observations; independent of report display caps."""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ProjectionLimits:
    max_bytes: int
    max_depth: int
    max_nodes: int
    max_facts: int
    max_statement_chars: int
    max_selectors: int
    max_pointer_chars: int


# Parsing protects the recorder; fact/display budgets protect the graph/report.
# Neither limit expresses a scientific acceptance threshold.
DEFAULT_LIMITS = ProjectionLimits(
    max_bytes=1_048_576,
    max_depth=64,
    max_nodes=20_000,
    max_facts=64,
    max_statement_chars=512,
    max_selectors=64,
    max_pointer_chars=1024,
)


@dataclass(frozen=True)
class ProjectedFact:
    pointer: str
    value_json: str

    @property
    def statement(self) -> str:
        location = json.dumps(self.pointer, ensure_ascii=False) if self.pointer else "JSON root"
        return f"{location} = {self.value_json}"


@dataclass(frozen=True)
class FactProjection:
    coverage: Literal["complete", "partial", "reference"]
    source_pointer: str
    facts: tuple[ProjectedFact, ...]
    omitted_leaves: int
    unrepresented_required: tuple[str, ...]
    reason: str | None

    @property
    def summary(self) -> str:
        source = "JSON root"
        if self.source_pointer:
            source = self.source_pointer
        detail = f"; причина: {self.reason}" if self.reason is not None else ""
        return (
            f"Проекция: покрытие={self.coverage}; "
            f"фактов={len(self.facts)}; непоказанных полей={self.omitted_leaves}; "
            f"непредставленных обязательных ссылок={len(self.unrepresented_required)}; источник={source}{detail}"
        )


class _Number(str):
    """Preserve the observed decimal token instead of rounding a source value."""


def _number(token: str) -> _Number:
    if not math.isfinite(float(token)):
        raise ValueError("nonfinite_number")
    return _Number(token)


def _nonfinite(token: str) -> Any:
    raise ValueError("nonfinite_constant")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _validate_shape(document: object, limits: ProjectionLimits) -> None:
    stack = [(document, 0, 0)]
    count = 0
    while stack:
        node, depth, path_chars = stack.pop()
        count += 1
        if count > limits.max_nodes:
            raise ValueError("node_budget")
        if depth > limits.max_depth:
            raise ValueError("depth_budget")
        if path_chars > limits.max_pointer_chars:
            raise ValueError("pointer_budget")
        if isinstance(node, dict):
            for key in node:
                key.encode("utf-8")
            stack.extend((value, depth + 1, path_chars + 1 + len(_escape(key))) for key, value in node.items())
        elif isinstance(node, list):
            stack.extend((value, depth + 1, path_chars + 1 + len(str(index))) for index, value in enumerate(node))
        elif isinstance(node, str):
            node.encode("utf-8")


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _leaves(document: object, pointer: str):
    """Breadth-first: shallow result fields precede bulk descendant arrays."""
    queue = deque([(document, pointer)])
    while queue:
        node, path = queue.popleft()
        if isinstance(node, dict):
            queue.extend((value, f"{path}/{_escape(key)}") for key, value in node.items())
        elif isinstance(node, list):
            queue.extend((value, f"{path}/{index}") for index, value in enumerate(node))
        else:
            yield path, node


def _pointer_tokens(pointer: str) -> list[str]:
    """RFC6901 tokens after the leading ``/`` (no ``str.split``)."""
    tokens: list[str] = []
    buf: list[str] = []
    for ch in pointer[1:]:
        if ch == "/":
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    tokens.append("".join(buf))
    return tokens


def _resolve(document: object, pointer: str) -> object:
    if not pointer:
        return document
    if not pointer.startswith("/"):
        raise ValueError("invalid_pointer")
    value = document
    for token in _pointer_tokens(pointer):
        # RFC 6901 only permits ~0 and ~1 escapes.
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 == len(token) or token[index + 1] not in "01":
                    raise ValueError("invalid_pointer")
                index += 1
            index += 1
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list):
            if not key.isascii() or not key.isdecimal() or (len(key) > 1 and key.startswith("0")):
                raise ValueError("invalid_array_pointer")
            value = value[int(key)]
        else:
            raise ValueError("pointer_not_container")
    return value


def _source(document: object) -> tuple[object, str]:
    """Unwrap only the journal ToolOutput / MCP CallToolResult contracts.

    MCP structuredContent is the machine-readable result. content[].text is
    deliberately not parsed as another JSON document or copied ahead of it.
    Arbitrary application dictionaries aren't searched for promising keys.
    """
    node, pointer = document, ""
    while isinstance(node, dict):
        if (
            isinstance(node.get("result"), dict)
            and type(node.get("content_digest")) is str
            and set(node).issubset({"result", "content_digest", "files"})
        ):
            node, pointer = node["result"], pointer + "/result"
            continue
        if isinstance(node.get("content"), list) and set(node).issubset(
            {"content", "structuredContent", "isError", "_meta"}
        ):
            if "isError" in node and type(node["isError"]) is not bool:
                raise ValueError("mcp_error_flag_invalid")
            if any(not isinstance(block, dict) or type(block.get("type")) is not str for block in node["content"]):
                raise ValueError("mcp_content_invalid")
            if node.get("isError") is True:
                raise ValueError("mcp_tool_error")
            if "structuredContent" not in node:
                raise ValueError("mcp_structured_content_missing")
            structured = node["structuredContent"]
            if not isinstance(structured, dict):
                raise ValueError("mcp_structured_content_invalid")
            node, pointer = structured, pointer + "/structuredContent"
            continue
        break
    return node, pointer


def reference_projection(reason: str, required_pointers: tuple[str, ...]) -> FactProjection:
    return FactProjection("reference", "", (), 0, required_pointers, reason)


def project_facts(
    raw: bytes,
    *,
    required_pointers: tuple[str, ...] = (),
    limits: ProjectionLimits = DEFAULT_LIMITS,
) -> FactProjection:
    """Strict source parse → exact pointer/value observations plus honest coverage.

    Required references (including failed prediction checks) have priority.
    They are selectors, never trusted values/digests: every fact is resolved
    again from the actual CAS document. Nothing here confers claim validity.
    """
    if len(raw) > limits.max_bytes:
        return reference_projection("byte_budget", required_pointers)
    if len(required_pointers) > limits.max_selectors or any(
        len(p) > limits.max_statement_chars for p in required_pointers
    ):
        return reference_projection("selector_budget", required_pointers)
    parse_errors: list[BaseException] = []
    document = None
    source = None
    source_pointer = ""
    try:
        document = json.loads(
            raw, parse_int=_Number, parse_float=_number, parse_constant=_nonfinite, object_pairs_hook=_object
        )
        _validate_shape(document, limits)
        source, source_pointer = _source(document)
    except (ValueError, UnicodeError, RecursionError) as exc:
        parse_errors.append(exc)
    if parse_errors:
        refuse = parse_errors[0]
        # Explicit refusal: reason string + unmet selectors (same as before).
        return reference_projection(
            f"parse_refused:{type(refuse).__name__}:{str(refuse)[:80]}",
            required_pointers,
        )
    if len(source_pointer) > limits.max_statement_chars // 2:
        return reference_projection("source_pointer_budget", required_pointers)

    source_leaves = dict(_leaves(source, source_pointer))
    selected: dict[str, ProjectedFact] = {}
    requirements: dict[str, int] = {}
    required_scalars: dict[str, object] = {}
    required_containers: dict[str, object] = {}
    for pointer in dict.fromkeys(required_pointers):
        resolved: list[object] = []
        resolve_errors: list[BaseException] = []
        try:
            resolved.append(_resolve(document, pointer))
        except (ValueError, KeyError, IndexError) as exc:
            resolve_errors.append(exc)
        if resolve_errors:
            # Zero resolved leaves; selector stays unmet; observe other selectors.
            requirements[pointer] = 0
            continue
        value = resolved[0]
        leaves = dict(_leaves(value, pointer))
        requirements[pointer] = len(leaves)
        if isinstance(value, (dict, list)):
            required_containers.update(leaves)
        else:
            required_scalars.update(leaves)
    for pointer, value in (*required_scalars.items(), *required_containers.items(), *source_leaves.items()):
        if pointer in selected:
            continue
        rendered = str(value) if isinstance(value, _Number) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        fact = ProjectedFact(pointer, rendered)
        if len(fact.statement) > limits.max_statement_chars or len(selected) >= limits.max_facts:
            continue
        selected[pointer] = fact
    facts = tuple(selected.values())
    unrepresented = tuple(
        pointer
        for pointer, count in requirements.items()
        if not count or sum(path == pointer or path.startswith(pointer + "/") for path in selected) != count
    )
    omitted = len(source_leaves.keys() - selected.keys())
    coverage: Literal["partial", "complete"] = "partial" if omitted or unrepresented else "complete"
    return FactProjection(coverage, source_pointer, facts, omitted, unrepresented, None)
