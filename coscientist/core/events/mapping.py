"""Strict mapping access helpers for fail-closed journal/runtime code.

Prefer ``m[key]`` (KeyError → ``JournalFormatError`` at the call site) for
required fields. Use ``optional_item`` only when absence is a meaningful
structural None — never to invent domain defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

K = TypeVar("K")
V = TypeVar("V")


def optional_item(m: Mapping[K, V], key: K) -> V | None:
    """Return ``m[key]`` when present; otherwise ``None`` (no invented default)."""
    if key not in m:
        return None
    return m[key]


def require_item(m: Mapping[K, V], key: K, *, what: str) -> V:
    """Return ``m[key]`` or raise ``JournalFormatError`` naming ``what``."""
    from coscientist.core.events.errors import JournalFormatError

    try:
        return m[key]
    except KeyError as e:
        raise JournalFormatError(f"{what}: missing {key!r}") from e


def optional_mapping(value: object) -> Mapping[Any, Any] | None:
    """Return ``value`` when it is a mapping; otherwise ``None``."""
    if isinstance(value, Mapping):
        return value
    return None
