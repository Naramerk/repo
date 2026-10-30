loop.py (PR11 loop materialization): dropped 3 provable `getattr` fallbacks —
`runtime.plans` (dict initialized in `StudyRuntime.__init__`, machine.py),
`study.max_tasks` (declared pydantic field `int = Field(default=64, ge=1)`,
domain.py; `or 64` kept verbatim), `runtime.catalog` (class-declared,
required constructor kwarg) — and narrowed `capped_scope_questions(runtime)`
from `Any` to `StudyRuntime` (all callers — `machine.view`,
`finalize._build_rich_payload`, tests — pass a real runtime). No behavior
change: every attribute is guaranteed by the type on the whole call chain
(CodeGraph callers/impact incl. api/experimental `StudyRuntime` subclasses).

Markers kept deliberately, each with reason:

- L61 `audit-ok[field_default]` (`LOOP_REASON_ERROR: Final[Literal["error"]]`):
  pre-existing; offline audit heuristic flags Literal field defaults.
- L432 `audit-ok[except_silent_return]`: pre-existing; the `except
  PlanNeedRefuse` arm journals LoopRefused via `_refuse_loop` before
  returning, which the heuristic cannot see.
- L125 `getattr(runtime, "_loop_state", None)`: lazy-init helper also called
  from `fold_loop_*` whose `runtime` stays `Any` — their only caller is
  `local_project._project_local` (file outside this task's edit scope)
  passing `self: LocalProjectMixin`, a mixin that declares `_loop_state` but
  never initializes it; absence is real for the mixin family, default is
  reachable by design.
- L553, L609 `getattr(runtime, "_loop_capped", None) or {}`, L688
  `getattr(runtime, "_loop_done", None) or set()`: same duck-typed
  reopen/replay contract as the identical pattern used throughout
  local_project.py; `or {}` / `isinstance` guards left untouched.
- L854 `getattr(runtime, "cancel_task", None)` + `callable()` guard:
  `cancel_task` is a duck-typed capability — LifecycleMixin defines a
  NotImplementedError stub and partial runtime variants may lack the real
  method; removal would turn an honest skip into AttributeError.
- L985 `getattr(t, "task_key", "")`: `t` is a `dict | object` row from an
  untrusted journal payload; both shapes are handled explicitly by design.
- L1021, L1063 `getattr(...)` lazy-init in `fold_loop_capped` /
  `fold_loop_completed`: `runtime: Any` replay entry points that create the
  holder attributes when missing.
- L1100 `getattr(runtime, "_loop_capped", None) or {}` in
  `capped_scope_questions`: body intentionally untouched (isinstance guard
  covers legacy/non-dict replay state).
- `fold_loop_*(runtime: Any, payload: dict[str, Any])` signatures: cannot be
  narrowed without editing `local_project.py` (its `self` is
  `LocalProjectMixin`, not `StudyRuntime`) — file outside this task's scope.
- JSON-shaped `dict[str, Any]` annotations (`_loop_state` return, emit
  payloads, `capped_scope_questions` return/`out`): heterogeneous dicts
  crossing module boundaries into files outside this task; `object` would
  force isinstance-restructuring or edits there.

Marker counts in loop.py: `audit-ok` 2 → 2, `Any` 11 → 11 (all remaining
sites justified above; one is the English word "Any" in a docstring),
`getattr` 12 → 9, `cast(` 0 → 0, `type: ignore` 0 → 0.
