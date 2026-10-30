cleanup(ext-expruntime2): replace the last provable Any in `coscientist/experimental/runtime.py`

- `_pending_costly_events: dict[str, dict[str, Any]]` →
  `dict[str, dict[str, object]]` — replaced (not narrowed), synchronized on
  BOTH sides of the annotation pair in one commit: the instance annotation in
  `experimental/runtime.py` `__init__` and the class-level declaration on
  `CostlyHitlMixin` (`experimental/hitl_costly.py`). The stored dicts are
  `HitlDecided` payload copies (str / float / None values), so `object` is
  the honest element type. The only writer (`hitl_costly.py`,
  `dict(payload)`) and the only reader (`settle_delegated_spend`, which
  passes `dict(pending)` into `settle(event: dict[str, Any], ...)`) type-
  check under mypy 1.19.1 with no new ignores. Annotations are strings under
  `from __future__ import annotations`: no runtime change, no behavior
  change. Chain verified with `codegraph sync`, `codegraph query
  StudyRuntime`, `codegraph callers open_study_runtime` (construction via
  `api/study_service.open_study_runtime`; tests build the runtime only
  through `open_runtime(..., runtime_cls=...)`); attribute usages enumerated
  by marker grep (`_pending_costly_events`, 4 hits: both declarations, one
  write, one read/pop in `hitl_costly.py`; no test fakes touch it), sources
  read via lean-ctx.
- `from typing import Any` dropped from `experimental/runtime.py` (unused
  after the replacement).

Kept as in base, with reasons:

- `# type: ignore[misc]` on the class line: load-bearing — with it removed
  mypy 1.19.1 fails with `Definition of "store" in base class "StudyRuntime"
  is incompatible with definition in base class "LifecycleMixin"` [misc]
  (same reason as the identical ignore in `coscientist/api/runtime.py`);
  re-verified on this branch by removing it locally.
- `getattr(arts, "get_meta", None)` in `view()`: `arts` is `self.artifacts`,
  declared `Any | None` in `core/execution/machine.py` (duck-typed object,
  outside this task's bounds), so a direct `.get_meta` could raise
  AttributeError on objects without the attribute; the `callable(lookup)`
  guard below stays byte-identical.

Counters for `coscientist/experimental/runtime.py` (вхождения / occurrences,
`grep -oE | wc -l`, base = origin/cleanup/refactor):
`Any` 2 → 0, `getattr` 1 → 1, `type: ignore` 1 → 1, `cast(` 0 → 0,
`audit-ok` 0 → 0. Paired file `experimental/hitl_costly.py`: `Any` 6 → 5
(the paired annotation), `getattr` 0 → 0, `type: ignore` 1 → 1, `cast(` 0 → 0.
No new markers anywhere.
