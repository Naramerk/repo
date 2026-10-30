Removed 3 dead `# type: ignore[...]` comments from
`tests/checks/test_macaque41_failclosed.py`: `[call-arg]` on
`sb.limits_from_check(**base)` and `[arg-type]` on the two multi-line
`CodeCheck(` calls in `test_codecheck_toggles_required_strict_bool_and_catalog_explicit`
(the error, if any, is reported on the argument line, not on the call line).
All three are reported as unused by mypy 1.19.1 `--warn-unused-ignores` in
both default mode and `--check-untyped-defs`; the error set (line-insensitive)
is base = head in both modes. Comment-only change; arguments, expected errors
and assertions are unchanged.

Kept: the other 6 ignores (`:282`, `:448`, `:450`, `:518`, `:519`, `:899`) are
unused in default mode but suppress real errors under `--check-untyped-defs`
(SimpleNamespace as `Popen`, deliberate bad kwargs/types in negative tests),
so they are live. The single `getattr` occurrence is a string literal inside a
negative assertion (`'getattr(runtime, "catalog"' not in src`) and stays.

Marker counts for the file (вхождения, `grep -o | wc -l`, base
`origin/cleanup/refactor`): `Any` 0 -> 0, `getattr` 1 -> 1,
`type: ignore` 9 -> 6, `cast(` 0 -> 0, `audit-ok` 0 -> 0.

Call chains checked with CodeGraph (`codegraph callers limits_from_check`,
`codegraph node CodeCheck`); comment-only change, no caller affected.
