"""API-owned StudyRuntime: lifecycle (create/accept) composed onto the machine shell.

Only ``coscientist.api`` may import ``core.lifecycle`` (import-linter). ``machine``
does not — so executor/agents → machine cannot reach ``_ingest_gate`` via lifecycle.
"""

from __future__ import annotations

from coscientist.core.lifecycle import LifecycleMixin
from coscientist.core.machine import StudyRuntime as MachineRuntime


class StudyRuntime(MachineRuntime, LifecycleMixin):  # type: ignore[misc]
    """Intake create_study / accept_plan / expand_plan + core state machine.

    MachineRuntime is listed first so ``_append`` / ``cancel_task`` resolve to
    the real implementations rather than LifecycleMixin's ``NotImplementedError``
    stubs. ``type: ignore[misc]``: LifecycleMixin attribute stubs vs concrete store.
    """


__all__ = ["StudyRuntime"]
