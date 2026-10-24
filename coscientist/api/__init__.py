"""HTTP-facing study API surface (DESIGN §9). Core must not import this package."""

from coscientist.api.errors import (
    AfterSeqRefuse,
    ApiRefuse,
    EventWireRefuse,
    StudyIdRefuse,
    StudyNotFound,
    StudyStoreMismatch,
)
from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.api.study_service import DataRootStudyService
from coscientist.api.views import (
    EventWire,
    event_to_wire,
    get_study,
    list_study_events,
    require_after_seq,
    require_study_id,
)

__all__ = [
    "AfterSeqRefuse",
    "ApiRefuse",
    "CreateStudyBody",
    "DataRootStudyService",
    "EventWire",
    "EventWireRefuse",
    "StudyIdRefuse",
    "StudyNotFound",
    "StudyStoreMismatch",
    "event_to_wire",
    "get_study",
    "list_study_events",
    "post_studies",
    "require_after_seq",
    "require_study_id",
]
