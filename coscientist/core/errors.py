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


class ArtifactCorrupt(CoreError):
    """Stored blob sha256 does not match the Artifact record."""


class ArtifactRejected(CoreError):
    """Artifact registration rejected (e.g. attempt already closed)."""

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
