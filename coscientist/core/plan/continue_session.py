"""Continue-session / fork planning (DESIGN §3.6).

Durable child journal materialization lives in ``fork_journal`` (called from
experimental ``HitlForkMixin.fork``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.ids import study_id_from_idempotency_key
from coscientist.core.execution.domain import Clarification, TaskSpec

# Untargeted clarification restarts SPEC + PLAN + descendants (§3.6 п.3).
_UNTARGETED_SEEDS: frozenset[str] = frozenset({"SPEC", "PLAN"})


class ContinueSessionRefuse(Exception):
    """Typed refuse — no silent defaults / invented subgraph."""

    def __init__(self, code: str, detail: str) -> None:
        if not code.strip() or not detail.strip():
            raise ValueError("ContinueSessionRefuse needs non-blank code and detail")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}")


def _require_parent_id(parent_id: object) -> str:
    if not isinstance(parent_id, str) or not parent_id.strip():
        raise ContinueSessionRefuse("parent_id_required", "parent_id must be non-blank")
    return parent_id


class ForkPartition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    parent_id: str = Field(min_length=1)
    affected: frozenset[str]
    inherited: frozenset[str]
    digests: Mapping[str, str]
    idem_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _disjoint_cover(self) -> ForkPartition:
        keys = set(self.digests)
        if self.affected | self.inherited != keys:
            raise ValueError("affected∪inherited must equal digests keys")
        if self.affected & self.inherited:
            raise ValueError("affected and inherited must be disjoint")
        return self


class ForkSessionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    parent_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    affected: tuple[str, ...]
    inherited: tuple[str, ...]
    idem_key: str = Field(min_length=1)
    digests: Mapping[str, str]

    @classmethod
    def from_partition(cls, part: ForkPartition, *, child_id: str) -> ForkSessionRecord:
        if not isinstance(child_id, str) or not child_id.strip():
            raise ContinueSessionRefuse("child_id_required", "child_id must be non-blank")
        return cls(
            parent_id=part.parent_id,
            child_id=child_id,
            affected=tuple(sorted(part.affected)),
            inherited=tuple(sorted(part.inherited)),
            idem_key=part.idem_key,
            digests=dict(part.digests),
        )


def task_spec_digest(spec: TaskSpec) -> str:
    return sha256_hex(canonical_json(spec.model_dump(mode="json")))


def clarifications_digest(clarifications: Sequence[Clarification], *, nonce: object) -> str:
    if not isinstance(nonce, str):
        raise ContinueSessionRefuse("nonce_must_be_str", f"got {type(nonce).__name__}")
    payload = [c.model_dump(mode="json") for c in clarifications] + [nonce]
    return sha256_hex(canonical_json(payload))


def fork_idem_key(
    parent_id: object,
    clarifications: Sequence[Clarification],
    *,
    nonce: object = "",
) -> str:
    """``fork:{parent}:{jcs_sha256(clarifications)}:{nonce}`` (§3.6 п.6).

    ``nonce`` defaults to ``""`` (empty-string replay → same child_id); pass a
    non-empty str for a fresh run with the same clarification text.
    """
    parent = _require_parent_id(parent_id)
    return f"fork:{parent}:{clarifications_digest(clarifications, nonce=nonce)}:{nonce}"


def fork_child_id(idem_key: object) -> str:
    """Deterministic child study_id from fork idem_key (§3.6 п.6; survives restart)."""
    if not isinstance(idem_key, str) or not idem_key.startswith("fork:"):
        raise ContinueSessionRefuse("idem_key_required", "expects fork:… idem_key")
    return study_id_from_idempotency_key(idem_key)


def _require_clarifications(clarifications: object) -> tuple[Clarification, ...]:
    if not isinstance(clarifications, Sequence) or isinstance(clarifications, (str, bytes)):
        raise ContinueSessionRefuse("clarifications_must_be_sequence", f"got {type(clarifications).__name__}")
    if len(clarifications) == 0:
        raise ContinueSessionRefuse("clarifications_required", "need ≥1 clarification")
    out: list[Clarification] = []
    for i, raw in enumerate(clarifications):
        if not isinstance(raw, Clarification):
            raise ContinueSessionRefuse("clarification_type", f"index {i}: got {type(raw).__name__}")
        if not raw.text.strip():
            raise ContinueSessionRefuse("clarification_text_blank", f"index {i} blank")
        out.append(raw)
    return tuple(out)


def _tasks_by_key(tasks: object) -> dict[str, TaskSpec]:
    by_key: dict[str, TaskSpec] = {}
    if isinstance(tasks, Mapping):
        for k, v in tasks.items():
            if not isinstance(k, str) or not k:
                raise ContinueSessionRefuse("task_key_type", f"map key must be non-empty str, got {type(k).__name__}")
            if not isinstance(v, TaskSpec):
                raise ContinueSessionRefuse("task_type", f"expected TaskSpec, got {type(v).__name__}")
            by_key[k] = v
    elif isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)):
        for spec in tasks:
            if not isinstance(spec, TaskSpec):
                raise ContinueSessionRefuse("task_type", f"expected TaskSpec, got {type(spec).__name__}")
            if spec.task_key in by_key:
                raise ContinueSessionRefuse("duplicate_task_key", f"task_key {spec.task_key!r} duplicated")
            by_key[spec.task_key] = spec
    else:
        raise ContinueSessionRefuse("task_type", f"expected Mapping/Sequence, got {type(tasks).__name__}")
    if not by_key:
        raise ContinueSessionRefuse("tasks_required", "parent has no tasks to fork")
    for key, spec in by_key.items():
        if spec.task_key != key:
            raise ContinueSessionRefuse("task_key_mismatch", f"map key {key!r} != {spec.task_key!r}")
    return by_key


def _seed_targets(clarifications: Sequence[Clarification], *, known: frozenset[str]) -> set[str]:
    seeds: set[str] = set()
    untargeted = False
    for c in clarifications:
        if c.target is None:
            untargeted = True
            continue
        if c.target not in known:
            raise ContinueSessionRefuse("unknown_target", f"target {c.target!r} is not a task_key")
        seeds.add(c.target)
    if untargeted:
        present = _UNTARGETED_SEEDS & known
        if not present:
            raise ContinueSessionRefuse(
                "untargeted_requires_spec_or_plan",
                "target=None requires SPEC and/or PLAN",
            )
        seeds |= present
    return seeds


def _closure(seeds: set[str], deps: Mapping[str, Sequence[str]]) -> set[str]:
    affected = set(seeds)
    changed = True
    while changed:
        changed = False
        for key, depends_on in deps.items():
            if key not in affected and any(d in affected for d in depends_on):
                affected.add(key)
                changed = True
    return affected


def partition_fork(
    *,
    parent_id: object,
    tasks: object,
    clarifications: object,
    nonce: object = "",
    stale_required_keys: object = (),
) -> ForkPartition:
    parent = _require_parent_id(parent_id)
    clarified = _require_clarifications(clarifications)
    by_key = _tasks_by_key(tasks)
    known = frozenset(by_key)
    if isinstance(stale_required_keys, (str, bytes)) or not isinstance(stale_required_keys, Iterable):
        raise ContinueSessionRefuse(
            "stale_required_keys_type",
            f"expected Iterable[str], got {type(stale_required_keys).__name__}",
        )
    stale = frozenset(stale_required_keys)
    if stale - known:
        raise ContinueSessionRefuse(
            "unknown_stale_key",
            f"stale not in parent: {sorted(stale - known)}",
        )
    seeds = _seed_targets(clarified, known=known) | set(stale)
    deps = {k: tuple(spec.depends_on) for k, spec in by_key.items()}
    affected = frozenset(_closure(seeds, deps))
    return ForkPartition(
        parent_id=parent,
        affected=affected,
        inherited=frozenset(known - affected),
        digests={k: task_spec_digest(spec) for k, spec in by_key.items()},
        idem_key=fork_idem_key(parent, clarified, nonce=nonce),
    )


def same_spec_for_inherit(parent: TaskSpec, child: TaskSpec) -> bool:
    return parent.task_key == child.task_key and task_spec_digest(parent) == task_spec_digest(child)
