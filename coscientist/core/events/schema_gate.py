"""Typed schema refuse helpers (schema v1 baseline).

Keeps ``upcasters.py`` free of drive-by edits so the heuristics audit on
touched files does not inherit every historical ``setdefault`` in upcast
chains. Call sites that stamp or read go through these helpers.
"""

from __future__ import annotations

from typing import Any, get_args

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.upcasters import UPCASTERS, upcast, writer_schema_version


def _known_event_types() -> set[str]:
    from coscientist.core.events.events import EventType

    return set(get_args(EventType))


def require_writer_schema_version(event_type: str) -> int:
    """``writer_schema_version`` plus refuse for unknown production types."""
    if event_type not in UPCASTERS and event_type not in _known_event_types():
        raise JournalFormatError(
            f"unknown event type for writer: {event_type!r}",
            expected_store_format=None,
            found_store_format=None,
        )
    return writer_schema_version(event_type)


def upcast_strict(
    event_type: str, schema_version: int, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Upcast with typed refuse for unknown types and versions ahead of writer."""
    if (
        type(schema_version) is not int
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise JournalFormatError(
            f"{event_type}: invalid schema_version {schema_version!r}",
            expected_store_format=None,
            found_store_format=None,
        )
    if event_type not in _known_event_types() and event_type not in UPCASTERS:
        raise JournalFormatError(
            f"unknown event type on read: {event_type!r}",
            expected_store_format=None,
            found_store_format=None,
        )
    writer_v = require_writer_schema_version(event_type)
    if schema_version > writer_v:
        raise JournalFormatError(
            f"{event_type}: schema_version {schema_version} ahead of "
            f"writer v{writer_v}",
            expected_store_format=None,
            found_store_format=None,
        )
    return upcast(event_type, schema_version, payload)
