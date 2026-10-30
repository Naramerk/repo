cleanup(ext-hitl): drop 6 provable getattr and 4 Any in `coscientist/experimental/hitl.py`

- 6x `getattr` on `HitlRequest` fields → direct attribute access
  (`expand_after_task` x2, `payload_ref` x2, `open` x2): every field is
  declared on `HitlRequest` (domain.py), and all construction sites go
  through the validating constructor or `model_validate` (CodeGraph: hitl.py,
  proposed_check.py, runtime.py, test_pr20_proposed_check.py — no
  `model_construct`), so the getattr defaults were unreachable; truth tables
  unchanged (the `or ""` / `not live.open` semantics are identical because the
  default values matched the declared field defaults).
- Dead `attempts: dict[str, dict[str, Any]]` stub removed: no method of
  `HitlForkMixin` (nor its base `CostlyHitlMixin`) reads `self.attempts`; the
  only host (`experimental/runtime.py`) gets the attribute from
  `MachineRuntime.__init__` (machine.py declares
  `self.attempts: dict[str, dict[str, Any]] = {}`).
- 2x local payload annotations `dict[str, Any]` → `dict[str, object]`
  (`decided_payload` in `answer_hitl`, `payload` in `_answer_proposed_check`);
  values are known heterogeneous scalars, `EventEnvelope.payload` accepts it.

Kept as in base (no honest fix without behavior/API change or new
ignore/cast):

- `plans: dict[str, dict[str, Any]]` — narrowing to `dict[str, dict[str, object]]`
  fails mypy: `max(viable, key=lambda p: p["revision"])` requires a comparable
  key type and the `type(...) is int` guard inside the list comprehension does
  not propagate narrowing; matches the canonical annotation in lifecycle.py.
- 4x `expansion: dict[str, Any] | None` (`answer_hitl` is public API; callers
  in api/hitl.py pass arbitrary human/JSON payload dicts) — a TypedDict would
  narrow the API.
- 4x `type: ignore[arg-type]` (`kind=kind` str → `HitlGateKind` literal;
  3x mixin `self` → core runtime parameter on `refuse_expansion` /
  `expand_plan`) — all live per `mypy --warn-unused-ignores`.
- 5x `getattr(self, "_hitl_requests"/"_hitl_decisions"/"_check_proposals", …)`
  — attributes are not declared on the mixin; hosts set them conditionally
  (runtime.py, machine.py, proposed_check.py), so the defaults are reachable.
- 2x `hasattr(self, "hitl")` guards — defensive duck-typing for hosts without
  the attribute; removing them would change behavior for such hosts.
