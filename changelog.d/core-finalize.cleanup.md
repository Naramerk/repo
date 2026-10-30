finalize: precise types and dead-duck getattr removal — no behavior change.

Removed without behavior change (attribute guaranteed by the object's class on
every reachable path; default unreachable):

- `finalize`: `getattr(runtime.store, "study_id", None)` → `runtime.store.study_id`
  (store is `SqliteEventStore`, which sets `study_id` unconditionally in
  `__init__`; `ports.EventStore` declares `study_id: str`).
- `_artifact_lookup`: `getattr(ex, "artifacts", None)` → `ex.artifacts`
  (`runtime.executor` is `Executor | None`; `Executor.__init__` always sets
  `self.artifacts`; only real `Executor` instances are bound — `bind_executor`
  is called only from `make_executor`).
- `_primary_report_path`: `getattr(study, "as_of", None)` → `study.as_of`
  (`Study.as_of: datetime | None = None` is a schema field present on every
  instance; the `study is not None` guard is kept).
- `_primary_report_path`: `getattr(runtime, "results", None)` → `runtime.results`
  (`StudyRuntime.results` is set unconditionally in `__init__` and is already
  accessed directly earlier in the same call chain).

Typed instead of `Any` (exact types, no new ignores/casts):

- `_artifact_lookup` returns `Callable[[str], Artifact | None] | None` — the
  exact parameter type declared by both consumers (`dispositions`,
  `report_hypothesis_verdicts`) and by `ports.ArtifactStore.get_meta`.
- `_artifact_store` returns `LocalArtifactStore | None` — every production and
  test assignment to `runtime.artifacts` is `LocalArtifactStore` or `None`
  (`runtime_factory` default; no caller passes a custom store).
- `_safe_custom_checks` takes `StudyRuntime` (its only caller passes the
  runtime typed `StudyRuntime`; the callee `custom_checks_report` accepts it).

Kept, with reasons:

- finalize.py:617 `getattr(arts, "get_meta", None)` in `_artifact_lookup`:
  `arts` is `Any | None` because `StudyRuntime.artifacts` is `Any | None` in
  machine.py (file owned by another workstream); the default is reachable
  (store may be `None` or an object without `get_meta`) — this duck-typing is
  the function's documented purpose.
- finalize.py:719 `getattr(plan, "questions", None)`: `_load_plans` returns a
  dict, so iteration yields `str` keys; the getattr default is reachable and
  load-bearing (direct attribute access would raise on non-dict values).
- finalize.py:292, 334 `# type: ignore[arg-type]`: dead only because
  `p: dict[str, Any]` (the `EventEnvelope.payload` contract) hides the hole;
  removing them is forbidden while the payload type stays `dict[str, Any]`.
- `dict[str, Any]` payload/disposition annotations (report payloads,
  `_append_*` helpers, `qdisp`, `hypothesis_verdicts`, `_load_plans`): mirror
  upstream contracts (`EventEnvelope.payload: dict[str, Any]`,
  `dispositions()` → `dict[str, dict[str, Any]]`); narrowing them would need
  new ignores/casts.
- finalize.py:453 audit-ok[broad_except] in `_put_last_resort_report`:
  pre-existing legacy totality rule (report packaging must never sink
  finalize); removal would change behavior.
