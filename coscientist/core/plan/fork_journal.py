"""Durable continue-session fork: child journal + lineage (DESIGN §3.6)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from coscientist.core.claims.graph import GraphNode
from coscientist.core.events.events import (
    STUDY_CREATED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import task_id
from coscientist.core.events.schema_versions import ARTIFACT_REGISTERED_SCHEMA_VERSION
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    Artifact,
    Catalog,
    Clarification,
    Study,
    Task,
    TaskOutcome,
    TaskState,
)
from coscientist.core.execution.machine import StudyRuntime
from coscientist.core.plan.continue_session import (
    ContinueSessionRefuse,
    ForkSessionRecord,
)
from coscientist.core.plan.study_node import study_kind_attrs


def data_root_for_runtime(store: SqliteEventStore) -> Path:
    """``events.db`` lives at ``data_root/<study_id>/events.db``."""
    return store.db_path.resolve().parent.parent


def _require_parent_study(parent: StudyRuntime) -> Study:
    study = parent.study
    if study is None:
        raise ContinueSessionRefuse("parent_study_required", "parent has no Study projection")
    return study


def _require_catalog(parent: StudyRuntime) -> Catalog:
    catalog = getattr(parent, "catalog", None)
    if not isinstance(catalog, Catalog):
        raise ContinueSessionRefuse(
            "catalog_required",
            "parent runtime has no catalog; cannot open child journal",
        )
    return catalog


def _task_by_key(parent: StudyRuntime) -> dict[str, Task]:
    out: dict[str, Task] = {}
    for task in parent.tasks.values():
        key = task.spec.task_key
        if key in out:
            raise ContinueSessionRefuse("duplicate_task_key", f"task_key {key!r} duplicated")
        out[key] = task
    return out


def resolve_inherit_done(
    record: ForkSessionRecord,
    by_key: Mapping[str, Task],
) -> tuple[frozenset[str], frozenset[str]]:
    """Split partition.inherited into DONE-inheritable vs must-rerun.

    DESIGN §3.6 p.5: inherited tasks are created DONE. Parent tasks that are not
    DONE cannot be honestly inherited — they are re-created PENDING (no invented
    completion).
    """
    inherit_done: set[str] = set()
    pending_inherit: set[str] = set()
    for key in record.inherited:
        parent_task = by_key.get(key)
        if parent_task is None:
            raise ContinueSessionRefuse("inherited_task_missing", f"parent lacks task_key {key!r}")
        if parent_task.state == TaskState.DONE and parent_task.outcome is not None:
            inherit_done.add(key)
        else:
            pending_inherit.add(key)
    return frozenset(inherit_done), frozenset(pending_inherit)


def collect_inherited_artifact_ids(
    *,
    inherit_done: frozenset[str],
    dep_outputs: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    """Map inherited task_key → named artifact_id refs from parent (no invention)."""
    refs: dict[str, dict[str, str]] = {}
    for key in sorted(inherit_done):
        raw = dep_outputs.get(key)
        if not isinstance(raw, Mapping) or not raw:
            continue
        cleaned: dict[str, str] = {}
        for name, art_id in raw.items():
            if not isinstance(name, str) or not name:
                raise ContinueSessionRefuse(
                    "artifact_ref_name_type",
                    f"task {key!r}: output name must be non-empty str",
                )
            if not isinstance(art_id, str) or not art_id.strip():
                raise ContinueSessionRefuse(
                    "artifact_ref_id_type",
                    f"task {key!r} output {name!r}: artifact_id must be non-blank str",
                )
            cleaned[name] = art_id
        if cleaned:
            refs[key] = cleaned
    return refs


def link_inherited_artifact(
    parent_arts: LocalArtifactStore,
    child_arts: LocalArtifactStore,
    artifact_id: str,
) -> Artifact:
    """Copy CAS bytes + meta into child arts; refuse if parent content is missing.

    Does not invent bytes — only copies an existing parent artifact by id.
    """
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ContinueSessionRefuse("artifact_id_required", "artifact_id must be non-blank")
    meta = parent_arts.get_meta(artifact_id)
    if meta is None:
        raise ContinueSessionRefuse(
            "inherited_artifact_missing",
            f"parent has no artifact {artifact_id!r}",
        )
    try:
        content = parent_arts.read_cas(meta.sha256)
    except FileNotFoundError as exc:
        raise ContinueSessionRefuse(
            "inherited_artifact_cas_missing",
            f"parent CAS missing for {artifact_id!r} digest {meta.sha256}",
        ) from exc
    digest = child_arts.put_cas(content)
    if digest != meta.sha256:
        raise ContinueSessionRefuse(
            "inherited_artifact_digest_mismatch",
            f"copied digest {digest} != parent {meta.sha256}",
        )
    linked = meta.model_copy(update={"uri": f"file://{child_arts.cas / digest}"})
    (child_arts.meta / f"{artifact_id}.json").write_text(
        linked.model_dump_json(),
        encoding="utf-8",
    )
    return linked


def _child_study(parent: Study, *, child_id: str, now: datetime) -> Study:
    return parent.model_copy(
        update={
            "study_id": child_id,
            "parent_study_id": parent.study_id,
            "created_at": now,
        }
    )


def _inherited_task_event(
    *,
    child_id: str,
    parent_task: Task,
    now: datetime,
) -> EventEnvelope:
    key = parent_task.spec.task_key
    tid = task_id(child_id, key)
    child_task = parent_task.model_copy(
        update={
            "task_id": tid,
            "state": TaskState.DONE,
            "outcome": parent_task.outcome or TaskOutcome.SUCCESS,
            "inherited_from": parent_task.task_id,
            # Attempt ids remain valid provenance pointers into the parent journal.
            "attempts": list(parent_task.attempts),
        }
    )
    return EventEnvelope(
        study_id=child_id,
        type="TaskInherited",
        entity_key=tid,
        idem_key=make_idem("TaskInherited", key),
        payload=child_task.model_dump(mode="json"),
        ts=now,
        schema_version=writer_schema_version("TaskInherited"),
    )


def _pending_task_event(
    *,
    child_id: str,
    parent_task: Task,
    now: datetime,
) -> EventEnvelope:
    key = parent_task.spec.task_key
    tid = task_id(child_id, key)
    child_task = parent_task.model_copy(
        update={
            "task_id": tid,
            "state": TaskState.PENDING,
            "outcome": None,
            "reason": None,
            "attempts": [],
            "inherited_from": None,
        }
    )
    return EventEnvelope(
        study_id=child_id,
        type="TaskCreated",
        entity_key=tid,
        idem_key=make_idem("TaskCreated", tid),
        payload=child_task.model_dump(mode="json"),
        ts=now,
        schema_version=TASK_CREATED_SCHEMA_VERSION,
    )


async def materialize_fork_journal(
    parent: object,
    record: ForkSessionRecord,
    *,
    clarifications: Sequence[Clarification],
    nonce: str = "",
) -> ForkSessionRecord:
    """Create or resume the child study journal for ``record`` (idempotent).

    Writes ``StudyCreated`` (with ``parent_study_id``), ``ForkCreated``, then
    ``TaskInherited`` / ``TaskCreated`` per partition. Re-invoking with the same
    ``fork_idem_key`` reopens the child and relies on event idem_keys — no
    double-create. Parent journal is never mutated.
    """
    if not isinstance(parent, StudyRuntime):
        raise ContinueSessionRefuse(
            "parent_runtime_type",
            f"expected StudyRuntime, got {type(parent).__name__}",
        )
    parent_study = _require_parent_study(parent)
    if record.parent_id != parent_study.study_id:
        raise ContinueSessionRefuse(
            "parent_id_mismatch",
            f"record.parent_id {record.parent_id!r} != study {parent_study.study_id!r}",
        )
    if not isinstance(nonce, str):
        raise ContinueSessionRefuse("nonce_must_be_str", f"got {type(nonce).__name__}")
    catalog = _require_catalog(parent)
    by_key = _task_by_key(parent)
    for key in record.digests:
        if key not in by_key:
            raise ContinueSessionRefuse("partition_task_missing", f"parent lacks task_key {key!r}")

    inherit_done, pending_inherit = resolve_inherit_done(record, by_key)
    affected_pending = frozenset(record.affected) | pending_inherit
    dep_outputs = parent.dep_outputs
    artifact_refs = collect_inherited_artifact_ids(
        inherit_done=inherit_done,
        dep_outputs=dep_outputs,
    )

    data_root = data_root_for_runtime(parent.store)
    child_id = record.child_id
    now = parent.clock.now()

    from coscientist.core.execution.runtime_factory import close_runtime, open_runtime

    runtime_cls = type(parent)
    child_rt = await open_runtime(
        data_root,
        child_id,
        catalog=catalog,
        clock=parent.clock,
        owner=parent.owner,
        runtime_cls=runtime_cls,
    )
    try:
        prior = await child_rt.store.read_all()
        existing_idem = {ev.idem_key for ev in prior}
        existing_keys = {t.spec.task_key for t in child_rt.tasks.values()}

        def _need(idem_key: str) -> bool:
            return idem_key not in existing_idem

        # Stable StudyCreated.created_at (parent birth) — write-once via idem set.
        child_study = _child_study(
            parent_study,
            child_id=child_id,
            now=parent_study.created_at,
        )
        events: list[EventEnvelope] = []
        study_idem = make_idem("StudyCreated", child_id)
        if _need(study_idem):
            events.append(
                EventEnvelope(
                    study_id=child_id,
                    type="StudyCreated",
                    entity_key=child_id,
                    idem_key=study_idem,
                    payload=child_study.model_dump(mode="json"),
                    ts=now,
                    schema_version=STUDY_CREATED_SCHEMA_VERSION,
                )
            )
        graph_idem = make_idem("GraphNodeUpserted", f"{child_id}:study")
        if _need(graph_idem):
            events.append(
                EventEnvelope(
                    study_id=child_id,
                    type="GraphNodeUpserted",
                    entity_key=f"{child_id}:study",
                    idem_key=graph_idem,
                    payload=GraphNode(
                        node_id=f"{child_id}:study",
                        kind="study",
                        study_id=child_id,
                        ref_id=child_id,
<<<<<<< HEAD
                        # Same required study attrs as LifecycleMixin.create_study
                        # (+ optional parent_study_id for fork lineage).
                        attrs={
                            "request": child_study.request,
                            "goal": {"statement": child_study.request},
                            "profile": child_study.profile,
                            "catalog_version": str(child_study.catalog_version),
                            "parent_study_id": parent_study.study_id,
                        },
=======
                        attrs=study_kind_attrs(
                            child_study, parent_study_id=parent_study.study_id
                        ),
>>>>>>> f5bd8e6 (fix(plan): one study-node attrs shape for create and fork)
                        created_at=parent_study.created_at,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=writer_schema_version("GraphNodeUpserted"),
                )
            )
        # ForkCreated is write-once for this idem_key. Parent task set may grow
        # between re-forks (same clarifications); never Conflict on digests.
        if _need(record.idem_key):
            events.append(
                EventEnvelope(
                    study_id=child_id,
                    type="ForkCreated",
                    entity_key=child_id,
                    idem_key=record.idem_key,
                    payload={
                        "parent_id": record.parent_id,
                        "child_id": child_id,
                        "idem_key": record.idem_key,
                        "affected": list(record.affected),
                        "inherited": list(record.inherited),
                        "digests": dict(record.digests),
                        "clarifications": [c.model_dump(mode="json") for c in clarifications],
                        "nonce": nonce,
                    },
                    ts=now,
                    schema_version=writer_schema_version("ForkCreated"),
                )
            )

        parent_arts = parent.artifacts
        child_arts = child_rt.artifacts
        if artifact_refs:
            if parent_arts is None or child_arts is None:
                raise ContinueSessionRefuse(
                    "artifacts_required",
                    "inherited artifact refs present but artifact store missing",
                )
            if not isinstance(child_arts, LocalArtifactStore):
                raise ContinueSessionRefuse(
                    "child_artifacts_type",
                    f"expected LocalArtifactStore, got {type(child_arts).__name__}",
                )
            seen_ids: set[str] = set()
            for _key, named in artifact_refs.items():
                for _name, art_id in named.items():
                    if art_id in seen_ids:
                        continue
                    seen_ids.add(art_id)
                    art_idem = make_idem("ArtifactRegistered", art_id)
                    if not _need(art_idem):
                        continue
                    linked = link_inherited_artifact(parent_arts, child_arts, art_id)
                    events.append(
                        EventEnvelope(
                            study_id=child_id,
                            type="ArtifactRegistered",
                            entity_key=art_id,
                            idem_key=art_idem,
                            payload=linked.model_dump(mode="json"),
                            ts=now,
                            schema_version=ARTIFACT_REGISTERED_SCHEMA_VERSION,
                        )
                    )

        # Partial TaskInherited resume (§3.7); never flip existing PENDING rows.
        for key in sorted(inherit_done):
            if key in existing_keys:
                continue
            events.append(_inherited_task_event(child_id=child_id, parent_task=by_key[key], now=now))
        for key in sorted(affected_pending):
            if key in existing_keys:
                continue
            events.append(_pending_task_event(child_id=child_id, parent_task=by_key[key], now=now))

        if events:
            await child_rt.append_events(events)
        child_rt.store.study_id = child_id
    finally:
        await close_runtime(child_rt)

    return record
