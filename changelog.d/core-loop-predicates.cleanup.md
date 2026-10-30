loop_predicates: dropped 7 provably dead `getattr` defaults and narrowed
3 `dict[str, Any]` annotations to `dict[str, object]` — behaviour and API
unchanged. Remaining markers, per line (loop_predicates.py), are load-bearing:

- 171: getattr(arts, "get_meta", None): runtime.artifacts is typed `Any | None`
  in machine.py (out of scope here); artifact-store shape is not guaranteed by
  any type, and the `callable(lookup)` fallback for stores without `get_meta`
  is live defensive contract.
- 328: getattr(st, "value", st): reachable default — check_results rows carry
  plain-string statuses (ReqStatus.MET.value) as proven by
  test_pred_criterion_met_reads_check_results; enums take the .value branch.
- 188, 192, 194, 197, 214, 241, 307, 308, 312: getattr left byte-identical to
  base: attribute presence is provable (declared on every StudyRuntime subclass
  and RuntimeClaim fields are typed str), but dropping the default rewrites the
  preserved `or {}` / `or ''` / optional-study semantics on touched lines, and
  the --vs-main audit then flags them (or_dict / or_empty / named_default);
  silencing would require new audit-ok, which is forbidden. Base form kept.
- 12, 32, 36, 88, 92: Any in the LoopPredicateFn protocol and eval_loop_predicate:
  plugin-facing pass-through contract; plugins receive an arbitrary runtime
  (tests pass None), so no precise type is provable without changing the API.
- 149, 163, 205, 232, 265, 282, 293, 300, 335: Any on runtime/ctx params of
  duck-typed helpers and built-ins: callers include unit-test fakes
  (SimpleNamespace, local RT classes) that are not StudyRuntime, and built-ins
  must stay call-compatible with the protocol whose runtime is Any.

Removed getattr lines had their defaults proven unreachable via CodeGraph
callers/impact: attributes are declared on every StudyRuntime subclass
(__init__) and on every test fake reaching those lines; TaskSpec.loop_id /
loop_iter are declared pydantic fields. validate_loop_predicate's params
narrowed to dict[str, object] — the honest type for untrusted plan params
checked by isinstance at runtime.
