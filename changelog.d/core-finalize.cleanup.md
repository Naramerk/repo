finalize: precise types for the artifact lookup helper — no behavior
change.

Counters (occurrences / вхождения, coscientist/): `Any` −1, `getattr` 0.

Typed instead of `Any` (exact types, no new ignores/casts):

- `_artifact_lookup` returns `Callable[[str], Artifact | None] | None` — the
  exact parameter type declared by both consumers (`dispositions`,
  `report_hypothesis_verdicts`) and by `ports.ArtifactStore.get_meta`.
- `_artifact_store` keeps its base `-> Any`: `runtime.artifacts` is `Any | None`
  and duck-typed stores reach finalize (e.g. `_BoomArts` in
  tests/test_finalize.py), so `LocalArtifactStore | None` would be stricter than
  what actually arrives.

Kept, with reasons:

- finalize.py:470 `getattr(runtime.store, "study_id", None)`: `study_id` is
  type-guaranteed (`SqliteEventStore` sets it unconditionally; `ports.EventStore`
  declares `study_id: str`) and the default is unreachable, but removing the
  getattr produces an `or ""` line that the `audit_heuristics --vs-main`
  `or_empty` rule flags; new audit-ok markers are forbidden, so per the rules
  the line stays byte-identical to base.
- finalize.py:614 `getattr(ex, "artifacts", None)`: `Executor.__init__` always
  sets `self.artifacts` and only real `Executor` instances are bound
  (`bind_executor` is called only from `make_executor`), but the direct
  attribute form is a `ternary_name` audit hit; stays as base.
- finalize.py:615 `getattr(arts, "get_meta", None)`: `arts` is `Any | None`
  because `StudyRuntime.artifacts` is `Any | None` in machine.py (file owned by
  another workstream); the default is reachable (store may be `None` or an
  object without `get_meta`) — this duck-typing is the function's documented
  purpose.
- finalize.py:717 `getattr(plan, "questions", None)`: `_load_plans` returns a
  dict, so iteration yields `str` keys; the getattr default is reachable and
  load-bearing (direct attribute access would raise on non-dict values).
- finalize.py:887 `getattr(study, "as_of", None)`: `Study.as_of` is a schema
  field present on every instance and the `study is not None` guard is kept,
  but the direct form is a `ternary_name` audit hit; stays as base.
- finalize.py:923 `getattr(runtime, "results", None)`: `StudyRuntime.results`
  is set unconditionally and already accessed directly earlier in the same
  call chain, but the `or {}` form is an `or_dict` audit hit; stays as base.
- finalize.py:290, 332 `# type: ignore[arg-type]`: dead only because
  `p: dict[str, Any]` (the `EventEnvelope.payload` contract) hides the hole;
  removing them is forbidden while the payload type stays `dict[str, Any]`.
- `_safe_custom_checks` keeps `runtime: Any`: its return type must mirror the
  callee `custom_checks_report(runtime: Any) -> list[dict[str, Any]]` (the
  callee lives in plan/proposed_check.py, owned by another workstream), so any
  edit to this signature would add a new `Any`-bearing line, which the
  new-markers gate forbids; the line stays byte-identical to base.
- `dict[str, Any]` payload/disposition annotations (report payloads,
  `_append_*` helpers, `qdisp`, `hypothesis_verdicts`, `_load_plans`): mirror
  upstream contracts (`EventEnvelope.payload: dict[str, Any]`,
  `dispositions()` → `dict[str, dict[str, Any]]`); narrowing them would need
  new ignores/casts.
- finalize.py:451 audit-ok[broad_except] in `_put_last_resort_report`:
  pre-existing legacy totality rule (report packaging must never sink
  finalize); removal would change behavior.
