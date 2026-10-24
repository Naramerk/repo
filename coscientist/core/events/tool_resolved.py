"""ToolResolved wire payload (DESIGN §6.3 / CAPABILITIES #8).

Models live in ``payload_schemas`` as ``WireModel`` (single wire path / PAYLOAD_MODELS).
This module re-exports for writers and tests.
"""

from __future__ import annotations

from coscientist.core.events.payload_schemas import MatchInfo, MatchKind, ToolResolvedPayload

__all__ = ["MatchInfo", "MatchKind", "ToolResolvedPayload"]
