core/claims/rubric.py: exact types instead of Any where provable —
freeze_task_rubrics(spec: TaskSpec) -> TaskSpec (every caller passes
list[TaskSpec]: plan_validate.freeze_plan_rubrics /
validate_plan_final_types_or_err, lifecycle.accept_plan normalizes to
TaskSpec before freezing); direct field access instead of dead-default
getattr on TaskSpec/OutputSpec (expected_outputs, rubric); replaced
`dict[str, Any]` with `dict[str, object]` for the digest payload and the
private row/check-result helpers `_is_rubric_check` / `_row_is_timeout_or_infra`
(only called from task_rubric_status; values are read opaquely via str() /
truthiness / equality). No behavior change; no new markers.

Counters (occurrences / вхождения, grep -o | wc -l, rubric.py):
`Any` 17 → 11, `getattr` 6 → 4, `cast(` 0 → 0, `type: ignore` 1 → 1,
`audit-ok` 0 → 0.

Kept markers (line: marker: why):

- 10: Any import: needed by the remaining annotations below.
- 147: plans: Any (question_final_types): callers pass heterogeneous plan
  JSON (dict-of-dict or list-of-dict); the body duck-dispatches (hasattr
  "values", isinstance list, per-item isinstance dict) and iterates arbitrary
  per-plan values ("questions" is a list at runtime). Callers' own
  `Mapping[str, Mapping[str, Any]]` annotations (dispositions) do not
  describe that JSON shape, and an `object` value type would force
  isinstance-narrowing rewrites of the iteration — a behavior change for
  non-dict iterables, which is forbidden.
- 170: type: ignore[assignment]: `ft` is a runtime-validated str stored as
  FinalType (Literal); mypy cannot express that narrowing from a frozenset
  membership check, and removing it requires cast() (forbidden).
- 174: results: Any and dict[str, Any] | None (result_row_for_attempt):
  result rows are heterogeneous JSON dicts; the fallback scans
  results.values() with isinstance(v, dict); an object-valued mapping would
  force behavior-risky narrowing rewrites, so Any stays the honest type.
- 190: effect: dict[str, Any]: the line's existing ternary
  (`raw_effect if isinstance(...) else {}`) carries the audit ternary_name
  heuristic; touching the annotation would demand a new audit-ok, which is
  forbidden.
- 230-235, 244, 246, 270, 273, 354-355 (task_rubric_status /
  task_rubrics_met): tasks/results stay Any and the getattr-with-default on
  t.spec / t.attempts / r.digest stays byte-identical to base. The tasks
  parameter feeds an unchanged duck-dispatch line
  (`(tasks.values() if hasattr(tasks, "values") else tasks) or []`): any
  precise container type makes mypy iterate Mapping keys (str) in the else
  branch, and that line cannot be rewritten either (ternary_name + or_empty
  heuristics would demand forbidden audit-ok; the rewrite itself would change
  behavior). With the parameter untyped, direct attribute access would raise
  AttributeError on an object lacking the attribute while base returns the
  default — forbidden behavior change (rule: typed-parameter-only getattr
  removal).
