"""Single source for hard-allowed fetch URI schemes."""

from __future__ import annotations

HARD_FETCH_SCHEMES: frozenset[str] = frozenset({"http", "https"})
