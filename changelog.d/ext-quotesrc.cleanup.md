tests/checks/test_quote_in_source.py: marker cleanup, no behavior change.
Marker counts for the file, in occurrences (grep -o | wc -l), base
origin/cleanup/refactor vs this branch: Any 12 -> 0 (import removed; 9 fake
tool call signatures `dict[str, Any]` replaced with `dict[str, object]`;
`list[Any]` in `_assert_single_study_finalized` replaced with
`list[StoredEvent]`; one fixture payload tuple annotation replaced with
`dict[str, object]`), getattr 1 -> 0, type: ignore 2 -> 0, cast 0 -> 0,
audit-ok 0 -> 0. No markers were added and none were left behind.

Details:

- `_catalog_quote` now returns the precise type `Catalog` that
  `load_catalog` already returns; the two `# type: ignore[arg-type]` on the
  `freeze_task_sandbox_limits` / `evaluate_task_checks` calls became dead and
  were removed. Annotation-only change: return value is the same object.
- The guarded assignment `if getattr(ex, "artifacts", None) is not None:` was
  replaced with a direct `ex.artifacts = arts`. Chain verified with CodeGraph:
  `attach_executor` (tests/helpers.py) returns a real `Executor` on all three
  paths (`codegraph callers make_executor`: StudyRuntime.make_executor at
  coscientist/core/execution/machine.py:158 constructs Executor;
  `bind_executor` types the parameter as Executor; the fallback constructs
  Executor directly), and `Executor.__init__` unconditionally sets the
  non-Optional `artifacts` attribute (coscientist/core/execution/executor.py),
  so the getattr default was unreachable and runtime behavior is identical.
- Fake tool `call(self, args, ctx)` signatures were replaced from
  `dict[str, Any]` with `dict[str, object]`: none of the nine fake bodies read
  `args`, and `dict[str, object]` still satisfies the `Tool` protocol
  (coscientist/tools/contract.py), whose `call` takes `dict[str, Any]`.
  Runtime annotations do not exist at runtime; nothing else changes.
- `_assert_single_study_finalized` was replaced from `list[Any]` with
  `list[StoredEvent]`: all three callers pass `await rt.store.read_all()`,
  which is typed `list[StoredEvent]` (coscientist/core/events/store.py).

Source reads were done via LeanCTX (`lean-ctx read ... -m signatures|lines`)
and call chains via CodeGraph (`codegraph callers`/`node`, local index).
mypy on the test file reports the same 28 pre-existing errors as the base
modulo line shifts (tests/ is outside the CI mypy scope); ruff check, ruff
format, and the file's 66 tests pass.
