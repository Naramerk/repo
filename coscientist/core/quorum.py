"""Fan-in quorum binding (M1 PR10 / S9) — explicit pools, never silent.

Pool keys (not equal ``quorum`` value):
- **Static** inputs with explicit quorum on one fan-in task → **one** shared pool.
- **Each foreach template** → its own pool (children of that template only).
- ``quorum is None`` → that input's members are all required (never softened).

``quorum: int`` (≥1) or ``"all"``. Zero counted bindings never yield provenance
``real``. Empty foreach members → miss.
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


def validate_quorum_specs(tasks: list[TaskSpec]) -> None:
    """Reject bad quorum and static pools where quorum > member count.

    Foreach child counts are unknown at plan accept — checked at expansion /
    bind (honest miss if quorum > children).
    """
    fe_keys = {t.task_key for t in tasks if t.foreach is not None}
    for t in tasks:
        static_q: list[InputSpec] = []
        for inp in t.inputs:
            q = inp.quorum
            if q is None:
                continue
            if q != "all" and (not isinstance(q, int) or q < 1):
                raise ValueError(
                    f"InputSpec.quorum must be >=1 or 'all' (task {t.task_key!r})"
                )
            if inp.from_task in fe_keys:
                continue
            static_q.append(inp)
        if not static_q:
            continue
        vals = {inp.quorum for inp in static_q}
        ints = {v for v in vals if v != "all"}
        if len(ints) > 1:
            raise ValueError(
                f"static InputSpec.quorum values must agree (task {t.task_key!r})"
            )
        q0 = next(iter(vals))
        if isinstance(q0, int) and q0 > len(static_q):
            raise ValueError(
                f"InputSpec.quorum {q0} > static pool size {len(static_q)} "
                f"(task {t.task_key!r})"
            )


def has_quorum_spec(inputs: list[InputSpec]) -> bool:
    return any(i.quorum is not None for i in inputs)


def group_needed(orig: InputSpec, n_members: int) -> int:
    """How many real/partial members this pool requires."""
    q = orig.quorum
    if q is None or q == "all":
        return n_members
    return max(1, int(q))


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
            source: Provenance = (
                source_raw if source_raw in VALID_PROVENANCE else "stub"
            )  # type: ignore[assignment]
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


def _bind_members(
    members: list[InputSpec],
    *,
    needed: int,
    miss_reason: str,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str],
    claim_provenance: dict[str, Provenance],
    verdict_snapshots: dict[str, dict[str, Any]],
) -> tuple[list[InputBinding], list[Provenance], list[Limitation]]:
    bindings: list[InputBinding] = []
    provs: list[Provenance] = []
    limitations: list[Limitation] = []
    ok: list[tuple[InputBinding, Provenance]] = []
    for spec in members:
        binding, source, lim = _try_bind_one(
            spec,
            dep_tasks=dep_tasks,
            dep_outputs=dep_outputs,
            dep_provenance=dep_provenance,
            known_claim_ids=known_claim_ids,
            claim_provenance=claim_provenance,
            verdict_snapshots=verdict_snapshots,
        )
        if lim is not None:
            limitations.append(lim)
            continue
        assert binding is not None and source is not None
        if source in ("real", "partial"):
            ok.append((binding, source))
        else:
            limitations.append(
                Limitation(
                    text=(
                        f"вход {binding.input} не засчитывается в кворум "
                        f"(provenance={source})"
                    ),
                    ref=spec.from_task or spec.ref,
                )
            )
    if len(ok) < needed:
        raise InputUnavailable(miss_reason)
    for b, s in ok:
        bindings.append(b)
        provs.append(s)
    return bindings, provs, limitations


def bind_input_groups(
    groups: list[tuple[InputSpec, list[InputSpec]]],
    *,
    foreach_from_tasks: set[str] | None = None,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str] | None = None,
    claim_provenance: dict[str, Provenance] | None = None,
    verdict_snapshots: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[InputBinding], Provenance, list[Limitation]]:
    """Bind with explicit pool keys: one static pool + one pool per foreach.

    Required (``quorum is None``) inputs stay fully required. Empty foreach
    members → miss. ``quorum > len(members)`` → miss (foreach checked at bind /
    expansion). Zero counted bindings never return provenance ``real``.
    """
    claims = known_claim_ids or set()
    claim_prov = claim_provenance or {}
    snapshots = verdict_snapshots or {}
    fe_from = foreach_from_tasks or set()

    bindings: list[InputBinding] = []
    provs: list[Provenance] = []
    limitations: list[Limitation] = []

    required: list[tuple[InputSpec, list[InputSpec]]] = []
    foreach_pools: list[tuple[InputSpec, list[InputSpec]]] = []
    static_quorum: list[tuple[InputSpec, list[InputSpec]]] = []

    for orig, members in groups:
        if orig.quorum is None:
            required.append((orig, members))
        elif orig.from_task in fe_from:
            foreach_pools.append((orig, members))
        else:
            static_quorum.append((orig, members))

    def _run(
        orig: InputSpec,
        members: list[InputSpec],
        *,
        needed: int,
        miss: str,
        empty_as: str,
    ) -> None:
        nonlocal bindings, provs, limitations
        label = orig.from_task or orig.ref or "?"
        if not members:
            limitations.append(
                Limitation(
                    text=f"вход {label} недоступен (foreach пуст)",
                    ref=orig.from_task or orig.ref,
                )
            )
            raise InputUnavailable(empty_as)
        if isinstance(orig.quorum, int) and orig.quorum > len(members):
            raise InputUnavailable(miss)
        b, p, lims = _bind_members(
            members,
            needed=needed,
            miss_reason=miss,
            dep_tasks=dep_tasks,
            dep_outputs=dep_outputs,
            dep_provenance=dep_provenance,
            known_claim_ids=claims,
            claim_provenance=claim_prov,
            verdict_snapshots=snapshots,
        )
        bindings.extend(b)
        provs.extend(p)
        limitations.extend(lims)

    for orig, members in required:
        _run(
            orig,
            members,
            needed=len(members),
            miss="input_unavailable",
            empty_as="input_unavailable",
        )

    for orig, members in foreach_pools:
        _run(
            orig,
            members,
            needed=group_needed(orig, len(members)),
            miss="quorum",
            empty_as="quorum",
        )

    if static_quorum:
        flat: list[InputSpec] = []
        for orig, members in static_quorum:
            flat.extend(members)
        q0 = static_quorum[0][0].quorum
        needed = len(flat) if q0 == "all" else max(1, int(q0 or 1))
        if isinstance(q0, int) and q0 > len(flat):
            raise InputUnavailable("quorum")
        if not flat:
            raise InputUnavailable("quorum")
        b, p, lims = _bind_members(
            flat,
            needed=needed,
            miss_reason="quorum",
            dep_tasks=dep_tasks,
            dep_outputs=dep_outputs,
            dep_provenance=dep_provenance,
            known_claim_ids=claims,
            claim_provenance=claim_prov,
            verdict_snapshots=snapshots,
        )
        bindings.extend(b)
        provs.extend(p)
        limitations.extend(lims)

    if not provs:
        raise InputUnavailable("quorum")
    return bindings, worst_provenance(*provs), limitations
