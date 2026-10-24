"""schema_version upcasters: immutable old payloads; read via upcasters[type][v] -> v+1.

Production event types are collapsed to writer v1 (PR no-hardcodes schema).
The registry mechanism remains for synthetic/unit-test chains; production
registers no upcasters. Pre-v1 journals are refused by store_format.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# type -> version -> function that upgrades payload from version to version+1
UPCASTERS: dict[str, dict[int, Upcaster]] = {}


def register_upcaster(event_type: str, from_version: int, fn: Upcaster) -> None:
    """Register ``from_version → from_version+1``. Duplicate (type, from_version) raises."""
    bucket = UPCASTERS.setdefault(event_type, {})
    if from_version in bucket:
        raise ValueError(
            f"duplicate upcaster for {event_type!r} from_version={from_version}"
        )
    bucket[from_version] = fn


def writer_schema_version(event_type: str) -> int:
    """Current writer version = 1 + max registered from_version (or 1 if none)."""
    versions = UPCASTERS.get(event_type) or {}
    if not versions:
        return 1
    return max(versions) + 1


def assert_upcaster_chain_continuous(event_type: str, writer_version: int) -> None:
    """Require upcasters for every v in 1..writer_version-1 (no gaps)."""
    if writer_version < 1:
        raise ValueError(f"{event_type}: writer_version must be >= 1")
    bucket = UPCASTERS.get(event_type) or {}
    missing = [v for v in range(1, writer_version) if v not in bucket]
    if missing:
        raise AssertionError(
            f"{event_type}: upcaster chain gaps before v{writer_version}: missing {missing}"
        )


def upcast(event_type: str, schema_version: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Apply chained upcasters until no more are registered."""
    version = schema_version
    data = dict(payload)
    while True:
        fn = UPCASTERS.get(event_type, {}).get(version)
        if fn is None:
            break
        data = fn(data)
        version += 1
    return version, data
