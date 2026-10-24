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

from coscientist.core.events.errors import JournalFormatError

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]
DictRowMutator = Callable[[dict[str, Any]], dict[str, Any]]

# type -> version -> function that upgrades payload from version to version+1
UPCASTERS: dict[str, dict[int, Upcaster]]
UPCASTERS = {}


def register_upcaster(event_type: str, from_version: int, fn: Upcaster) -> None:
    """Register ``from_version → from_version+1``. Duplicate (type, from_version) raises."""
    if not isinstance(from_version, int) or isinstance(from_version, bool) or from_version < 1:  # audit-ok[or_call]: coalesce existing call result; no invented literal
        raise ValueError(
            f"register_upcaster {event_type!r}: from_version must be int >= 1, "
            f"got {from_version!r}"
        )
    bucket = UPCASTERS.get(event_type)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if bucket is None:
        bucket = {}
        UPCASTERS[event_type] = bucket
    if from_version in bucket:
        raise ValueError(
            f"duplicate upcaster for {event_type!r} from_version={from_version}"
        )
    if from_version > 1 and (from_version - 1) not in bucket:
        raise ValueError(
            f"register_upcaster {event_type!r}: gap — missing from_version="
            f"{from_version - 1} before {from_version}"
        )
    bucket[from_version] = fn


def writer_schema_version(event_type: str) -> int:
    """Current writer version = 1 + max registered from_version.

    Unknown event types (not in ``EventType`` and not registered) refuse.
    Known types with an empty upcaster bucket are writer v1.
    """
    from typing import get_args

    from coscientist.core.events.events import EventType

    known = set(get_args(EventType))
    versions = UPCASTERS.get(event_type)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if versions is None:
        if event_type not in known:
            raise JournalFormatError(f"unknown event type for writer: {event_type!r}")
        return 1
    if not versions:
        return 1
    return max(versions) + 1


def assert_upcaster_chain_continuous(event_type: str, writer_version: int) -> None:
    """Require upcasters for every v in 1..writer_version-1 (no gaps)."""
    if writer_version < 1:
        raise ValueError(f"{event_type}: writer_version must be >= 1")
    # Refuse unknown types (same gate as writer_schema_version); empty bucket = v1.
    writer_schema_version(event_type)
    bucket = UPCASTERS.get(event_type)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if bucket is None:
        bucket = {}
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
    rows = out.get(field)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if not isinstance(rows, list):
        return out
    rewritten = []
    for item in rows:
        if isinstance(item, dict):
            rewritten.append(mutate(dict(item)))
        else:
            rewritten.append(item)
    out[field] = rewritten
    return out


def require_exact_int(value: object, *, what: str) -> int:
    """Require ``type(value) is int`` (rejects bool, str, float)."""
    if type(value) is not int:
        raise JournalFormatError(
            f"{what} must be exact int, got {type(value).__name__}: {value!r}"
        )
    return value


def _known_event_types() -> frozenset[str]:
    # Local import: events.py must not import this module at load time.
    from coscientist.core.events.events import EventType

    return frozenset(get_args(EventType))


def upcast(
    event_type: str, schema_version: object, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Apply chained upcasters from ``schema_version`` up to the writer version.

    Raises ``JournalFormatError`` when:
    - ``event_type`` is not a known ``EventType`` (and has no registered upcasters),
    - ``schema_version`` is not an exact ``int`` (bool/str/float rejected),
    - ``schema_version`` is below 1 or above the writer version,
    - the upcaster chain has a gap before the writer version.
    """
    known = _known_event_types()
    if event_type not in known and event_type not in UPCASTERS:
        raise JournalFormatError(f"unknown event type: {event_type!r}")

    version = require_exact_int(
        schema_version, what=f"{event_type}.schema_version"
    )
    if version < 1:
        raise JournalFormatError(
            f"{event_type}: schema_version must be >= 1, got {version}"
        )

    writer = writer_schema_version(event_type)
    if version > writer:
        raise JournalFormatError(
            f"{event_type}: schema_version {version} above writer {writer}"
        )

    data = dict(payload)
    bucket = UPCASTERS.get(event_type)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if bucket is None:
        bucket = {}
    while version < writer:
        fn = bucket.get(version)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if fn is None:
            raise JournalFormatError(
                f"{event_type}: upcaster chain gap at version {version} "
                f"(writer={writer})"
            )
        data = fn(data)
        version += 1
    return version, data
