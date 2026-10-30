core-expand-trigger: replaced `dict[str, Any]` with `dict[str, object]` on
`_result_row_for_attempt` (result rows are opaque folded fields read via
isinstance/str) and dropped the now-unused `typing.Any` import; replaced 7
provably-safe `getattr` default reads with direct attribute access. Call
chains verified with `codegraph callers`/`impact` (machine.py self-paths,
recover.py::recover, expand.py, foreach.py, experimental/hitl.py, and every
runtime_cls test passes a real StudyRuntime subclass) and `lean-ctx read` on
machine.py/domain.py declarations.

Counters (occurrences / вхождения, coscientist/core/plan/expand_trigger.py):
`Any` 2 → 0, `getattr` 16 → 9, `type: ignore` 0 → 0, `cast(` 0 → 0,
`audit-ok` 0 → 0.

Removed getattr (attribute declared on every StudyRuntime path, default
unreachable; machine.py `__init__` lines per machine.py base):
- 152, 193 `_agent_expansions` (declared int, machine.py:123);
- 160 `_replan_used_s` (declared float, machine.py:125);
- 211 `catalog` (required ctor kwarg, machine.py:62/94);
- 241 `_expand_requests` (declared dict, machine.py:127; the defensive
  isinstance branch is kept unchanged);
- 250 `_expand_pending` (declared set, machine.py:126; isinstance branch
  kept);
- 413 `task.reason` (declared `TaskReason | None` field, domain.py:607;
  the pydantic default None equals the dropped default for every Task,
  including model_construct, which fills field defaults).

Kept markers byte-identical to base, per line (expand_trigger.py):
- 98, 101, 369: `getattr(runtime, "_expansion_refusals" /
  "_expand_requests", default) or []` / `or {}`: the getattr default is
  fused with the `or` fallback; direct access keeping `or []`/`or {}` is
  an `or_dict` audit hit on a touched line (new audit-ok forbidden), and
  dropping the fallback rewrites the unexpected-input handling — base
  form kept.
- 294, 375, 393: same for `(getattr(runtime, "_expand_requests", None)
  or {}).get(plan_key)`.
- 296, 403: `getattr(task.outcome, "value", task.outcome)` — Task.outcome
  is `TaskOutcome | None` (domain.py:606); None takes the default branch.
- 414: `getattr(reason, "value", reason)` — Task.reason is
  `TaskReason | None`; None takes the default branch.
