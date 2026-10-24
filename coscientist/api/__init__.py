"""HTTP-facing study API (DESIGN §9). FastAPI: ``coscientist.api.app.create_app``."""

from coscientist.api.cancel import post_study_cancel, require_cancel_by
from coscientist.api.errors import (
    AfterSeqRefuse,
    ApiRefuse,
    CancelByRefuse,
    EventWireRefuse,
    StreamConfigRefuse,
    StudyIdRefuse,
    StudyStoreMismatch,
    StudyViewLoopRefuse,
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
    "CancelByRefuse",
    "CreateStudyBody",
    "DataRootStudyService",
    "EventWire",
    "EventWireRefuse",
    "StreamConfigRefuse",
    "StudyIdRefuse",
    "StudyStoreMismatch",
    "StudyViewLoopRefuse",
    "event_to_wire",
    "get_study",
    "list_study_events",
    "post_studies",
    "post_study_cancel",
    "require_after_seq",
    "require_cancel_by",
    "require_study_id",
]
