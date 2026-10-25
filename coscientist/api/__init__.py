"""HTTP-facing study API (DESIGN §9). FastAPI: ``coscientist.api.app.create_app``."""

from coscientist.api.app import AppDeps, bind_app_deps, create_app
from coscientist.api.cancel import bind_cancel_study, post_study_cancel, require_cancel_by
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
from coscientist.api.study_service import DataRootStudyService, bind_event_store_for
from coscientist.api.views import (
    EventWire,
    event_to_wire,
    get_study,
    list_study_events,
    parse_after_query,
    require_after_seq,
    require_study_id,
)

__all__ = [
    "AfterSeqRefuse",
    "ApiRefuse",
    "AppDeps",
    "CancelByRefuse",
    "CreateStudyBody",
    "DataRootStudyService",
    "EventWire",
    "EventWireRefuse",
    "StreamConfigRefuse",
    "StudyIdRefuse",
    "StudyStoreMismatch",
    "StudyViewLoopRefuse",
    "bind_app_deps",
    "bind_cancel_study",
    "bind_event_store_for",
    "create_app",
    "event_to_wire",
    "get_study",
    "list_study_events",
    "parse_after_query",
    "post_studies",
    "post_study_cancel",
    "require_after_seq",
    "require_cancel_by",
    "require_study_id",
]
