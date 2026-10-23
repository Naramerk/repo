"""Minimal InputBinding / dependency resolution (DESIGN §3.4)."""

from __future__ import annotations

from enum import StrEnum

from coscientist.core.domain import (
    InputBinding,
    InputSpec,
    Provenance,
    Task,
    TaskOutcome,
    TaskState,
)

PROVENANCE_RANK = {"real": 0, "partial": 1, "stub": 2}


class InputMode(StrEnum):
    USE_PARTIAL = "use_partial"
    SIMULATE = "simulate"
    FORBID = "forbid"


def worst_provenance(*values: Provenance) -> Provenance:
    """Least favorable under real < partial < stub."""
    return max(values, key=lambda p: PROVENANCE_RANK[p])


def normalize_input_mode(spec: InputSpec) -> InputMode:
    return InputMode(spec.mode)


class InputUnavailable(Exception):
    def __init__(self, reason: str = "input_unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


def bind_inputs(
    task: Task,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
) -> tuple[list[InputBinding], Provenance]:
    """
    Resolve TaskSpec.inputs.
    Raises InputUnavailable when mode=forbid and data missing / dep FAILED.
    """
    bindings: list[InputBinding] = []
    provs: list[Provenance] = ["real"]
    for spec in task.spec.inputs:
        mode = normalize_input_mode(spec)
        dep = dep_tasks.get(spec.from_task)
        if dep is None or dep.state != TaskState.DONE:
            raise InputUnavailable()
        arts = dep_outputs.get(spec.from_task, {})
        aid = arts.get(spec.output)
        dep_prov: Provenance = dep_provenance.get(spec.from_task, "real")

        if dep.outcome == TaskOutcome.FAILED and mode == InputMode.FORBID:
            raise InputUnavailable()

        if aid and dep.outcome == TaskOutcome.SUCCESS:
            source: Provenance = "real"
        elif aid and mode in {InputMode.USE_PARTIAL, InputMode.SIMULATE}:
            source = "partial"
        elif mode == InputMode.SIMULATE:
            source = "stub"
            aid = f"stub:{spec.from_task}:{spec.output}"
        else:
            raise InputUnavailable()

        if source != "stub":
            source = worst_provenance(source, dep_prov)
        bindings.append(InputBinding(input=spec.output, artifact_id=aid, source=source))
        provs.append(source)
    return bindings, worst_provenance(*provs)
