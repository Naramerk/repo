"""Reserved module name — do not import.

User-input registration lives in ``coscientist.core._ingest_gate`` and is
reachable only via ``api.studies`` → ``lifecycle``. This module intentionally
re-exports nothing (PR19 re-review: no ``put_user`` / ``user_ingest_scope``).
"""

from __future__ import annotations

__all__: list[str] = []
