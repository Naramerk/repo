"""HTTP-facing study API (DESIGN §9). FastAPI: ``coscientist.api.app.create_app``.

``create_app`` / ``AppDeps`` / ``bind_app_deps`` live in ``coscientist.api.app``
and are not re-exported here — package init must not require the ``api`` extra
(``fastapi``) so ``.[mcp]`` / ingest-tar stay importable.
"""

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
    "CancelByRefuse",
    "CreateStudyBody",
    "DataRootStudyService",
    "EventWire",
    "EventWireRefuse",
    "StreamConfigRefuse",
    "StudyIdRefuse",
    "StudyStoreMismatch",
    "StudyViewLoopRefuse",
    "bind_cancel_study",
    "bind_event_store_for",
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
