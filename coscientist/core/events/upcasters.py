"""schema_version upcasters: immutable old payloads; read via upcasters[type][v] -> v+1.

Schema v1 baseline (pre-release collapse): production event types have no upcasters;
``writer_schema_version(type)`` is 1. The register / continuous-chain machinery remains
for post-release evolution and is covered by tests on a synthetic dummy type.

Post-release rule: upcasters may only add **provably exact** values via
``map_spec_dict_list`` (or an equally mechanical rename of stored bytes). Guessed
defaults, clamps, inference from names/shape, or literal version fills require an
explicit migration — not an upcaster.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]
DictRowMutator = Callable[[dict[str, Any]], dict[str, Any]]

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

def map_spec_dict_list(
    spec: dict[str, Any],
    field: str,
    mutate: DictRowMutator,
) -> dict[str, Any]:
    """Return a copy of ``spec`` with dict rows under ``spec[field]`` passed through ``mutate``.

    Non-dict rows are left unchanged. Missing/non-list ``field`` leaves ``spec`` fields as-is
    (still returns a shallow copy of ``spec``).

    Post-release upcasters may only add provably exact values via this helper (e.g. a
    bijective rename from stored bytes). Guessed defaults require an explicit migration.
    """
    out = dict(spec)
    rows = out.get(field)
    if not isinstance(rows, list):
        return out
    rewritten: list[Any] = []
    for item in rows:
        if isinstance(item, dict):
            rewritten.append(mutate(dict(item)))
        else:
            rewritten.append(item)
    out[field] = rewritten
    return out

def _known_event_types() -> frozenset[str]:
    # Local import: events.py must not import this module at load time.
    from coscientist.core.events.events import EventType

    return frozenset(get_args(EventType))

def upcast(
    event_type: str, schema_version: int, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Apply chained upcasters from ``schema_version`` up to the writer version.

    Raises ``ValueError`` when:
    - ``event_type`` is not a known ``EventType`` (and has no registered upcasters),
    - ``schema_version`` is above the writer version,
    - the upcaster chain has a gap before the writer version.
    """
    known = _known_event_types()
    if event_type not in known and event_type not in UPCASTERS:
        raise ValueError(f"unknown event type: {event_type!r}")

    if schema_version < 1:
        raise ValueError(
            f"{event_type}: schema_version must be >= 1, got {schema_version}"
        )

    writer = writer_schema_version(event_type)
    if schema_version > writer:
        raise ValueError(
            f"{event_type}: schema_version {schema_version} above writer {writer}"
        )

    version = schema_version
    data = dict(payload)
    bucket = UPCASTERS.get(event_type) or {}
    while version < writer:
        fn = bucket.get(version)
        if fn is None:
            raise ValueError(
                f"{event_type}: upcaster chain gap at version {version} "
                f"(writer={writer})"
            )
        data = fn(data)
        version += 1
    return version, data
