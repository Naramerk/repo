"""Typed API refusals (DESIGN §9). Fail-closed — never invent missing studies or seq.

``StudyNotFound`` lives only in ``coscientist.core.events.errors`` (no re-export).
"""

from __future__ import annotations


class ApiRefuse(Exception):
    """Base for HTTP-layer typed refusals (map to 4xx at the transport)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StudyIdRefuse(ApiRefuse):
    """``study_id`` missing, non-str, empty, or padded with whitespace."""


class StudyStoreMismatch(ApiRefuse):
    """Path ``study_id`` ≠ ``EventStore.study_id`` or event wire ``study_id``."""


class AfterSeqRefuse(ApiRefuse):
    """``after`` query is not a non-negative StrictInt."""


class EventWireRefuse(ApiRefuse):
    """Stored event cannot be projected to the public wire DTO."""


class StudyViewLoopRefuse(ApiRefuse):
    """``DataRootStudyService.study_view`` called under a running event loop."""


class CancelByRefuse(ApiRefuse):
    """``by`` for POST cancel empty or whitespace-padded."""


class StudyRunRefuse(ApiRefuse):
    """POST/GET ``/studies/{id}/run`` typed refusal (fail-closed run lane)."""


class StudyRunFailed(ApiRefuse):
    """API-started run crashed before the study journal opened (maps to 5xx)."""


class StreamConfigRefuse(ApiRefuse):
    """WS stream poll interval not a positive finite float."""


class WsCloseReasonRefuse(ApiRefuse):
    """WebSocket close reason exceeds the protocol byte limit."""
