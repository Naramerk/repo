"""Typed schema refuse helpers (schema v1 baseline).

Keeps ``upcasters.py`` free of drive-by edits so the heuristics audit on
touched files does not inherit every historical ``setdefault`` in upcast
chains. Call sites that stamp or read go through these helpers.

Imports from ``upcasters`` are lazy to avoid import cycles with shares/events.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import get_args

from coscientist.core.events.errors import JournalFormatError

PayloadValidator = Callable[[dict[str, object]], dict[str, object]]


def _known_event_types() -> set[str]:
    from coscientist.core.events.events import EventType

    return set(get_args(EventType))


@lru_cache(maxsize=1)
def _payload_validators() -> dict[str, PayloadValidator]:
    """One real schema per ``EventType``. Missing registry key → fail-closed."""
    from coscientist.core.events.events import EventType
    from coscientist.core.events.payload_schemas import PAYLOAD_MODELS, validate_event_payload

    def _bound(et: str) -> PayloadValidator:
        def _fn(raw: dict[str, object]) -> dict[str, object]:
            return validate_event_payload(et, raw)

        return _fn

    reg: dict[str, PayloadValidator] = {t: _bound(t) for t in PAYLOAD_MODELS}
    missing = set(get_args(EventType)) - set(reg)
    if missing:
        raise RuntimeError(f"payload schemas missing EventTypes: {sorted(missing)}")
    return reg


def require_writer_schema_version(event_type: str) -> int:
    """``writer_schema_version`` plus refuse for unknown production types."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    if event_type not in UPCASTERS and event_type not in _known_event_types():
        raise JournalFormatError(
            f"unknown event type for writer: {event_type!r}",
            expected_store_format=None,
            found_store_format=None,
        )
    return writer_schema_version(event_type)


def upcast_strict(event_type: str, schema_version: int, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    """Upcast then apply registered payload schema (fail-closed if none)."""
    from coscientist.core.events.upcasters import UPCASTERS, upcast

    if type(schema_version) is not int or isinstance(schema_version, bool) or schema_version < 1:
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
            f"{event_type}: schema_version {schema_version} ahead of writer v{writer_v}",
            expected_store_format=None,
            found_store_format=None,
        )
    version, out = upcast(event_type, schema_version, payload)
    fn = _payload_validators().get(event_type)
    if fn is None:
        raise JournalFormatError(
            f"{event_type}: no payload validator registered",
            expected_store_format=None,
            found_store_format=None,
        )
    return version, fn(out)
