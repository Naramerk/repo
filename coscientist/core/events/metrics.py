"""In-process counters (no Prometheus dependency in core)."""

from __future__ import annotations

from collections import defaultdict

# late_write_rejected_total{event_type}
late_write_rejected_total: dict[str, int] = defaultdict(int)


def bump_late_write_rejected(event_type: str) -> None:
    late_write_rejected_total[event_type] += 1
