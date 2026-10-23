"""Fan-in quorum binding (M1 PR10 / S9).

A summary task starts when ≥ quorum inputs resolve to real/partial; the rest
become ``Limitation`` rows (Russian text) on the attempt / experiment node.
"""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import (
    VALID_PROVENANCE,
    InputBinding,
    InputSpec,
    Limitation,
    Provenance,
    Task,
    TaskOutcome,
    TaskSpec,
    TaskState,
    worst_provenance,
)
from coscientist.core.inputs import (
    InputMode,
    InputUnavailable,
    _claim_source,
    input_binding_digest,
    normalize_input_mode,
    verdict_binding_digest,
)


def task_quorum_needed(inputs: list[InputSpec]) -> int:
    """How many real/partial bindings are required to start.

    No ``quorum`` set → all inputs (legacy). Any ``"all"`` → all.
    Otherwise the minimum positive int declared (easier threshold wins when
    several inputs disagree — typically they share one value).
    """
    n = len(inputs)
    if n == 0:
        return 0
    explicit = [i.quorum for i in inputs if i.quorum is not None]
    if not explicit:
        return n
    if any(q == "all" for q in explicit):
        return n
    ints = [int(q) for q in explicit if q != "all"]
    if not ints:
        return n
    return max(0, min(ints))


def has_quorum_spec(inputs: list[InputSpec]) -> bool:
    return any(i.quorum is not None for i in inputs)


def validate_quorum_specs(tasks: list[TaskSpec]) -> None:
    for t in tasks:
        for inp in t.inputs:
            q = inp.quorum
            if q is None or q == "all":
                continue
            if not isinstance(q, int) or q < 0:
                raise ValueError(f"InputSpec.quorum must be >=0 or 'all' (task {t.task_key!r})")


def _try_bind_one(
    spec: InputSpec,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str],
    claim_provenance: dict[str, Provenance],
    verdict_snapshots: dict[str, dict[str, Any]],
) -> tuple[InputBinding | None, Provenance | None, Limitation | None]:
    """Bind one input or return a limitation explaining the gap."""
    kind = spec.kind
    try:
        if kind == "verdict":
            if not spec.ref:
                raise InputUnavailable("verdict_ref_missing")
            snap = verdict_snapshots.get(spec.ref)
            if snap is None:
                raise InputUnavailable("verdict_unavailable")
            source_raw = snap.get("provenance", "real")
            source: Provenance = source_raw if source_raw in VALID_PROVENANCE else "stub"  # type: ignore[assignment]
            digest = verdict_binding_digest(ref=spec.ref, snapshot=snap)
            binding = InputBinding(
                input=spec.ref,
                artifact_id="",
                source=source,
                kind="verdict",
                ref=spec.ref,
                digest=digest,
            )
            return binding, source, None
        if kind == "claim":
            if not spec.ref:
                raise InputUnavailable("claim_ref_missing")
            if spec.ref not in known_claim_ids:
                raise InputUnavailable("claim_unavailable")
            source = _claim_source(spec.ref, claim_provenance=claim_provenance)
            digest = input_binding_digest(
                kind="claim",
                input_name=spec.ref,
                artifact_id="",
                source=source,
                ref=spec.ref,
            )
            binding = InputBinding(
                input=spec.ref,
                artifact_id="",
                source=source,
                kind="claim",
                ref=spec.ref,
                digest=digest,
            )
            return binding, source, None

        # artifact
        if not spec.from_task or not spec.output:
            raise InputUnavailable("artifact_spec_incomplete")
        mode = normalize_input_mode(spec)
        dep = dep_tasks.get(spec.from_task)
        label = f"{spec.from_task}:{spec.output}"
        if dep is None or dep.state != TaskState.DONE:
            return (
                None,
                None,
                Limitation(
                    text=f"вход {label} недоступен (зависимость не завершена)",
                    ref=spec.from_task,
                ),
            )
        arts = dep_outputs.get(spec.from_task, {})
        aid = arts.get(spec.output)
        dep_prov: Provenance = dep_provenance.get(spec.from_task, "real")

        if dep.outcome == TaskOutcome.FAILED and mode == InputMode.FORBID:
            return (
                None,
                None,
                Limitation(
                    text=f"вход {label} недоступен (задача FAILED)",
                    ref=spec.from_task,
                ),
            )
        if dep.outcome == TaskOutcome.SKIPPED:
            return (
                None,
                None,
                Limitation(
                    text=f"вход {label} недоступен (задача SKIPPED)",
                    ref=spec.from_task,
                ),
            )

        if aid and dep.outcome == TaskOutcome.SUCCESS:
            source = "real"
        elif aid and mode in {InputMode.USE_PARTIAL, InputMode.SIMULATE}:
            source = "partial"
        elif mode == InputMode.SIMULATE:
            source = "stub"
            aid = f"stub:{spec.from_task}:{spec.output}"
        else:
            return (
                None,
                None,
                Limitation(
                    text=f"вход {label} недоступен (нет артефакта)",
                    ref=spec.from_task,
                ),
            )

        if source != "stub":
            source = worst_provenance(source, dep_prov)
        digest = input_binding_digest(
            kind="artifact",
            input_name=spec.output,
            artifact_id=aid or "",
            source=source,
            ref=None,
        )
        binding = InputBinding(
            input=spec.output,
            artifact_id=aid or "",
            source=source,
            kind="artifact",
            digest=digest,
        )
        return binding, source, None
    except InputUnavailable as e:
        ref = spec.from_task or spec.ref or ""
        return (
            None,
            None,
            Limitation(
                text=f"вход {ref or '?'} недоступен ({e.reason})",
                ref=ref or None,
            ),
        )


def bind_with_quorum(
    task: Task,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str] | None = None,
    claim_provenance: dict[str, Provenance] | None = None,
    verdict_snapshots: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[InputBinding], Provenance, list[Limitation]]:
    """Resolve inputs under quorum: ≥ needed real/partial, rest → limitations.

    Raises ``InputUnavailable`` when fewer than needed succeed (caller skips).
    Stub-only bindings do **not** count toward quorum (DESIGN: real/partial).
    """
    specs = list(task.spec.inputs)
    needed = task_quorum_needed(specs)
    claims = known_claim_ids or set()
    claim_prov = claim_provenance or {}
    snapshots = verdict_snapshots or {}

    bindings: list[InputBinding] = []
    provs: list[Provenance] = []
    limitations: list[Limitation] = []
    counted = 0

    for spec in specs:
        binding, source, lim = _try_bind_one(
            spec,
            dep_tasks=dep_tasks,
            dep_outputs=dep_outputs,
            dep_provenance=dep_provenance,
            known_claim_ids=claims,
            claim_provenance=claim_prov,
            verdict_snapshots=snapshots,
        )
        if lim is not None:
            limitations.append(lim)
            continue
        assert binding is not None and source is not None
        if source in ("real", "partial"):
            counted += 1
            bindings.append(binding)
            provs.append(source)
        else:
            # stub does not satisfy quorum — record as limitation, do not bind.
            limitations.append(
                Limitation(
                    text=(f"вход {binding.input} не засчитывается в кворум (provenance={source})"),
                    ref=spec.from_task or spec.ref,
                )
            )

    if counted < needed:
        raise InputUnavailable("quorum")
    if not provs:
        return bindings, "real", limitations
    return bindings, worst_provenance(*provs), limitations
