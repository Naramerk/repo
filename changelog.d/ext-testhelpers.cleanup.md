cleanup(ext-testhelpers): replaced Any with object in tests/helpers.py plan-blob helpers

tests/helpers.py: two parameter annotations replaced with `object` (annotations only;
`from __future__ import annotations` makes them lazy, runtime untouched):
`plan_revision` `blob: Mapping[str, Any]` -> `Mapping[str, object]` and
`append_plan_accepted` `payload: Mapping[str, Any]` -> `Mapping[str, object]`
(both keep their exact `type(x) is not int/str` checks and messages).
`Mapping` is covariant in the value type, so every caller's dict conforms.
Call chain: CodeGraph `callers plan_revision`, `callers append_plan_accepted`
(all in tests/test_g4_plan_need.py). mypy (default and `--check-untyped-defs`)
on tests/helpers.py and tests/test_g4_plan_need.py: error set identical to base.
ruff, gen_model_tables, mypy, lint-imports, audit_heuristics ok; pytest same as base.

Marker counts in tests/helpers.py (вхождения, `grep -o | wc -l`,
base `origin/cleanup/refactor`): Any 9 -> 7, getattr 6 -> 6, type: ignore 1 -> 1,
cast( 0 -> 0, audit-ok 0 -> 0.

Left (no audit-ok added):
- `active_plan_blob_for_tests` return `dict[str, Any]`: rule 21. With
  `dict[str, object]`, tests/test_g4_plan_need.py:419/462/502 index a
  `dict[str, dict[str, Any]]` with values read from the blob and get three new
  mypy `[index]` errors (CodeGraph `callers active_plan_blob_for_tests`), so
  the annotation stays.
- Six `getattr(rt, ...)` in `attach_executor` (artifacts / make_executor /
  study / max_attempts / _check_proposals / bind_executor): `rt` is
  duck-typed — the `RuntimeLike` Protocol guarantees only writer, clock,
  attempts, tasks, executor, `_project_local`. None of the accessed
  attributes is declared on the Protocol, and the defaults are reachable by
  design (the fallback branch for fakes without `make_executor` /
  `bind_executor` wires `ex.get_graph` and `rt.executor`). Direct access
  would raise AttributeError on conforming-but-minimal objects. Chain
  verified via `codegraph callers attach_executor` (229 call sites) and
  `codegraph callers record_success` (49).
- `ex._runtime_attempts = rt.attempts  # type: ignore[attr-defined]`:
  load-bearing. `Executor` does not declare `_runtime_attempts`; it is read
  at coscientist/core/execution/executor.py:812 via
  `getattr(self, "_runtime_attempts", None)` (found via
  `codegraph query "_runtime_attempts"` + `git grep -l _runtime_attempts`,
  no code lines). Dropping the assignment changes runtime behavior; dropping
  the ignore adds a mypy error. executor.py is core/ and out of scope.
  mypy `--warn-unused-ignores` does not flag this ignore (unused-ignore hits
  are only in pre-existing core files, out of scope).
- `catalog: Any | None` in `open_runtime` / `open_runtime_experimental`:
  mirrors the production factories, which declare `catalog: Any` themselves
  (coscientist/core/execution/runtime_factory.py and
  coscientist/experimental/runtime_factory.py, both read via
  `lean-ctx read ... -m signatures`). The ~60 `catalog=` call sites across
  the 68 helper-using test files are not all provably `Catalog`-conforming;
  a replacement with `Catalog | None` would be a hidden contract change, so
  the wider type stays.
- `**kwargs: Any` in both wrappers: forwarded to the production factories.
  Precise typing would require enumerating the factory parameters, moving
  the unexpected-keyword `TypeError` from the factory binding to the wrapper
  binding — an API/error-message change.
- `RuntimeLike.attempts: dict[str, dict[str, Any]]`: mirrors the production
  authority `StudyRuntime.attempts` (machine.py:88, same type). Replacing
  with `dict[str, dict[str, object]]` adds a new mypy error at
  `list(d.get("inputs") or [])` (helpers.py:191); a TypedDict cannot be
  proven against all 229 `attach_executor` call sites' fakes.
- The `from typing import Any` import (1 occurrence) feeds the remaining
  Any uses above.

Note: `ruff format --check tests/helpers.py` reports would-reformat spans on
lines untouched by this change, identically in base; CI runs `ruff check`
only, and reformatting unrelated lines is out of scope for this change.
