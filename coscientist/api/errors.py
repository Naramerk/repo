"""Typed API refusals (DESIGN §9). Fail-closed — never invent missing studies or seq."""

from __future__ import annotations


class ApiRefuse(Exception):
    """Base for HTTP-layer typed refusals (map to 4xx at the transport)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StudyIdRefuse(ApiRefuse):
    """``study_id`` missing, non-str, empty, or padded with whitespace."""


class StudyNotFound(ApiRefuse):
    """No study journal for ``study_id`` (or StudyService reports missing)."""

    def __init__(self, study_id: str, *, reason: str = "study_not_found") -> None:
        self.study_id = study_id
        super().__init__(reason)


class AfterSeqRefuse(ApiRefuse):
    """``after`` query is not a non-negative StrictInt."""


class EventWireRefuse(ApiRefuse):
    """Stored event cannot be projected to the public wire DTO."""
