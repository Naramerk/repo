cleanup(ext-rubric): drop 4 provable getattr and 7 Any in `coscientist/checks/kinds/rubric.py`

- 4x `getattr` → direct attribute access in `evaluate_output_spec_rubrics`:
  `spec` is annotated `TaskSpec` (sole caller `evaluate_task_checks` passes
  `TaskSpec`; CodeGraph callers + repo grep found no other production or test
  call site), and `tools_hint: list[str]` / `expected_outputs: list[OutputSpec]`
  are declared TaskSpec fields, `rubric: Rubric | None` / `required: bool = True`
  are declared OutputSpec fields — the getattr defaults are unreachable for the
  declared types, so no input can hit a missing attribute (no AttributeError
  change). `or []` on `expected_outputs` kept as-is.
- `spec: Any` → `TaskSpec` (see above).
- `output_rubric: Any | None` → `Rubric | None` (evaluate_rubric + _evaluate):
  every pass site hands a frozen `Rubric` or `None` (evaluate.py:835
  `rubric_from_proposal` result, evaluate.py:1184 `freeze_rubric` result,
  rubric.py internal `freeze_rubric` result); tests never pass it.
- `evaluate_code_check: Any | None` → `Callable[..., CheckResult] | None` (×3,
  codebase idiom, cf. `api/app.py`): every pass site hands the real
  `evaluate_code_check` (evaluate.py:523/839/1188/1257 and the local `_ecc`
  fallback import), return-typed `CheckResult`; a kw-exact callback Protocol
  would need a second `typing` import line, which neither ruff isort (I001
  merge) nor the newmarkers gate (no added `coscientist/` line may contain
  `Any`) allows together — Callable is the provable contract both gates
  accept.
- `_as_rubric(raw: Any)` → `raw: object`: the function takes arbitrary
  `model_validate` input; `object` is the honest top type, isinstance guard and
  body unchanged.

Counters for `coscientist/checks/kinds/rubric.py` (base = origin/cleanup/refactor,
verified with counts.sh): `Any` 11 → 4, `getattr` 4 → 0, `type: ignore` 0 → 0,
`cast(` 0 → 0, `audit-ok` 0 → 0 (no new).

Kept as in base (no honest fix without behavior/API change, new cast, or new
audit-ok):

- `artifacts: Any` ×3 (`evaluate_rubric`, `_evaluate`,
  `evaluate_output_spec_rubrics`) — judge-only paths legitimately run without a
  store: tests pass `artifacts=None` (test_pr13_rubric_final_type.py ×6) and
  `MagicMock()` (test_macaque_closeout.py); the honest `LocalArtifactStore |
  None` contradicts the declared `artifacts: LocalArtifactStore` of the nested
  `evaluate_code_check` call, so any narrowing needs a cast, a runtime branch,
  or a lie — left byte-identical to base.
