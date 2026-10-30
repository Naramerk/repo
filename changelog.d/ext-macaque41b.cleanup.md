Cleaned `tests/checks/test_macaque41_failclosed.py`: removed all 9 dead
`# type: ignore[...]` comments (`[arg-type]` on 6 lines, `[call-arg]` on 3
lines). Under the repo mypy config (`warn_unused_ignores = false`,
`files = ["coscientist", "bench"]`, tests outside the mypy scope) mypy does
not check the bodies of untyped test functions, so none of the 9 comments
suppressed an actual error: the file passes plain `mypy` identically before
and after (the only reported error is the pre-existing one in
`tests/helpers.py`), and `mypy --warn-unused-ignores` reports no unused
ignores afterwards. Call arguments, error expectations, and assertions are
unchanged; the only layout change is the `limits_from_check(**base, ...)`
call in
`test_limits_from_check_requires_every_argument_and_has_no_host_budget_default`,
re-exploded with a magic trailing comma so `ruff format` keeps every line
within 100 characters after the comment removal (the removed comment had
been anchoring the previous multi-line layout).

Marker counts for the file (occurrences, `grep -o | wc -l`), before -> after:
`Any` 0 -> 0, `getattr` 1 -> 1, `type: ignore` 9 -> 0, `cast(` 0 -> 0.

Kept marker: the single `getattr` occurrence is a string literal inside
`test_plan_freeze_sites_use_runtime_catalog_and_reraise_catalog_error`
(`assert 'getattr(runtime, "catalog"' not in src`), i.e. the negative
assertion that guards the plan freeze sites against `getattr` access in
production code; it is the audit itself, so it stays byte-identical.

Call chains re-checked with CodeGraph (`node` + `callers` for `_classify`,
`limits_from_check`, `sandbox_effect`; `codegraph sync` after the edit) —
comment-only change, no caller affected; sources read via LeanCTX.
