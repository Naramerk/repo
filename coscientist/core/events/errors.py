"""Core exception types."""


class CoreError(Exception):
    """Base for core errors."""


class CatalogError(CoreError):
    """Invalid or incomplete requirement catalog / empty required set."""


class Conflict(CoreError):
    """Idempotency key reused with a different payload."""


class Idempotent(CoreError):
    """Same idem_key and same payload — treat as no-op success."""

    def __init__(self, seq: int):
        self.seq = seq
        super().__init__(f"idempotent replay at seq={seq}")


class LeaseLost(CoreError):
    """Writer no longer holds the study lease (epoch fencing)."""


class AttemptClosedError(CoreError):
    """Attempt is closed; late writes are rejected."""


class StudyClosedError(CoreError):
    """Study is finalized; further journal writes are rejected (I3)."""

    def __init__(self, study_id: str, event_type: str = ""):
        self.study_id = study_id
        self.event_type = event_type
        super().__init__(
            f"study {study_id} is finalized; rejecting {event_type or 'write'}"
        )


def reject_write_after_finalized(finalized: bool, event_type: str) -> str | None:
    """Return ``StudyClosedError`` name when a post-finalize write must be rejected."""
    if finalized and event_type != "StudyFinalized":
        return "StudyClosedError"
    return None


class CASUnavailable(CoreError):
    """Content-addressed artifact store is not attached / unreachable."""


class ArtifactCorrupt(CoreError):
    """Stored blob sha256 does not match the Artifact record."""


class ArtifactRejected(CoreError):
    """Artifact registration rejected (e.g. attempt already closed)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class FetchToolReclassified(CoreError):
    """Journaled ``AttemptTainted`` claims ``is_fetch`` but the live registry does not.

    Replay must not raise through fold/open: callers catch this, skip counting
    the paired ``SourceFetched``, and journal ``FetchIntegrityRaised`` so the
    study can still finalize with ``internal_error`` in the report.
    """

    def __init__(self, tool: str, call_id: str = ""):
        self.tool = tool
        self.call_id = call_id
        super().__init__(
            f"AttemptTainted tool={tool!r} journaled is_fetch=True but current "
            f"registry has is_fetch=False (reclassified)"
        )


class SandboxInfraError(CoreError):
    """Sandbox timeout/OOM/unavailable — close attempt as infra failure.

    Never a host crash: the sandbox runner always returns a terminal status;
    executable CodeCheck kinds raise this so ``Executor.record`` can commit
    ``result_class=infra`` with a stable ``reason`` string.

    ``transient`` is set by the raiser (e.g. cgroup attach race) — never inferred
    from the reason string in core.
    """

    def __init__(self, reason: str, *, transient: bool):
        self.reason = reason
        self.transient = transient
        super().__init__(reason)


class CoreBugError(CoreError):
    """Unexpected core failure — neither infra nor scientific.

    Reported on the attempt/result path but does not count toward scientific
    MET/FAILED criteria and is not classified as sandbox infra.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class InvalidStatus(CoreError):
    """Value outside the allowed status enum."""


class HitlUnsupported(CoreError):
    """Core StudyRuntime cannot process a journal with open HITL gates.

    Core drops HITL: ``tick`` ignores HITL deadlines, ``view()`` reports
    ``open_hitl=0``, and there is no ``recover_after_idle``. Open an
    experimental runtime (pre-M2) for journals that still have open gates.
    """

    def __init__(self, request_ids: list[str]):
        if not request_ids:
            raise ValueError("HitlUnsupported requires at least one request_id")
        self.request_ids = list(request_ids)
        super().__init__(
            f"core StudyRuntime cannot open/recover journal with open HITL "
            f"gate(s): {', '.join(self.request_ids)}; "
            f"use coscientist.experimental.StudyRuntime"
        )
