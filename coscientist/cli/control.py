"""Owner-only interactive control port; transport never writes the journal."""

from typing import Protocol

from coscientist.agents.schemas import PlanOut
from coscientist.api.runtime import StudyRuntime


class StudyControl(Protocol):
    @property
    def runtime_cls(self) -> type[StudyRuntime]: ...

    async def review_plan(self, runtime: StudyRuntime, plan: PlanOut) -> bool: ...
