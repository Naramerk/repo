cleanup(ext-expruntime): drop provable Any/getattr in `coscientist/experimental/runtime.py`

- `__init__(*args: Any, **kwargs: Any)` → the precise signature of the only
  `super().__init__` target, `core.execution.machine.StudyRuntime.__init__`
  (`store: SqliteEventStore, writer: StudyWriter, *, catalog: Catalog,
  clock: Clock | None, registry: KindRegistry | None, max_attempts: int | None,
  owner: str`). The single production construction site
  (`core.execution.runtime_factory.open_runtime`) passes exactly these
  arguments (`codegraph callers StudyRuntime`/`open_runtime`, read with lean-ctx:
  tests build it only via `open_runtime(..., runtime_cls=...)`, which calls
  `cls(store, writer, catalog=, clock=, registry=, owner=)`); wrong-argument TypeErrors
  keep the same texts because both classes are named `StudyRuntime`.
- `getattr(HitlRequest, "model_fields", None)` → `HitlRequest.model_fields`:
  HitlRequest is a pydantic `Frozen(BaseModel)` subclass, so `model_fields` is
  a guaranteed ClassVar; the `or {}` fallback and the `if fields else dict(p)`
  branch are kept verbatim.
- `getattr(self, "_hitl_decisions", None)` → `self._hitl_decisions`: the
  attribute is declared and initialized in `MachineRuntime.__init__`
  (machine.py:129) and on `LocalProjectMixin`; the defensive `isinstance`
  guard below it is kept byte-identical.
- `view()`: `getattr(self, "artifacts" / "graph" / "results" /
  "question_disposals" / "artifact_kinds" / "catalog", None)` → direct
  attribute access — all six are initialized unconditionally in
  `MachineRuntime.__init__` (machine.py:82/89/94/95/105/107), so the `None`
  defaults are unreachable for any initialized instance.
- `_hitl_notices: list[dict[str, Any]]` → `list[dict[str, object]]`: the list
  is write-only (defined and appended here, read nowhere in coscientist/ or tests/,
  `git grep -c _hitl_notices`); its only source is `dict(ev.payload)`, and `object` is the honest
  element type ("replaced", not narrowed).

Kept as in base (no honest fix without a behavior/contract change or new
scope):

- `# type: ignore[misc]` on the class line: load-bearing — removing it fails
  mypy with `Definition of "store" in base class "StudyRuntime" is
  incompatible with definition in base class "LifecycleMixin"` (same reason
  as the identical ignore in `coscientist/api/runtime.py`); verified by
  removing it locally.
- `getattr(arts, "get_meta", None)` in `view()`: `arts` is `self.artifacts`,
  declared `Any | None` in the base machine (machine.py:95) — a duck-typed
  object, so direct `.get_meta` could raise AttributeError on objects without
  the attribute.
- `_pending_costly_events: dict[str, dict[str, Any]]`: annotation pair with
  `CostlyHitlMixin` (`experimental/hitl_costly.py:38` declares the same
  attribute `dict[str, dict[str, Any]]` and consumes the values as `Any` in
  `settle_delegated_spend`); changing only this side would desynchronize the
  pair, and that file is outside this task's bounds.

Counters for `coscientist/experimental/runtime.py` (вхождения / occurrences,
`grep -o | wc -l`, base = origin/cleanup/refactor, сверено с counts.sh):
`Any` 5 → 2, `getattr` 9 → 1, `type: ignore` 1 → 1, `cast(` 0 → 0,
`audit-ok` 0 → 0 (no new). mypy on the affected test files: identical error
set to base (tests/ is outside the mypy CI scope); no new errors.
