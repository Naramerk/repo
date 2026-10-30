cleanup(ext-schemas): drop one Any local annotation and two dead ignores in agents/schemas.py

`coscientist/agents/schemas.py` (behaviour unchanged; API, wire schemas, error
codes, defaults, logs intact — local variable annotations are erased at
runtime, and both values flow only into `pydantic.create_model` kwargs, so the
generated models and JSON schemas are identical):

- `plan_intent_model_for`: local `tasks_field: Any` (line 432) replaced with
  a plain assignment — mypy infers the RHS type.
- `route_type: Any` (line 423) and `name_type: Any` (line 694): the
  `type: ignore[valid-type]` on both lines was dead. mypy 1.19.1 in the project
  environment with `--warn-unused-ignores` reports it unused in both modes,
  because the explicit `Any` annotation already stops the valid-type check.
  The ignores are removed. `Any` stays: an `object` annotation fails
  `create_model` (arg-type, not a TypeForm), and dropping the annotation
  restores the valid-type error.

Call chains checked with CodeGraph before the edit:
`codegraph callers -f schemas.py plan_intent_model_for` (18 callers —
production `coscientist/agents/adapter.py:399`, the rest tests),
`codegraph callers -f schemas.py remote_coder_task_model_for` (5 callers —
production `coscientist/agents/adapter.py:551`, the rest tests),
`codegraph impact plan_intent_model_for` (92 symbols). Source read via
`lean-ctx read coscientist/agents/schemas.py --fresh -m signatures|lines:N-M`.

Marker occurrences in `coscientist/agents/schemas.py`
(`grep -o '<marker>' file | wc -l`, base `origin/cleanup/refactor`):

- `Any`: 5 → 4 вхождения
- `getattr`: 0 → 0 вхождений
- `type: ignore`: 7 → 5 вхождений
- `cast(`: 0 → 0 вхождений
- `audit-ok`: 7 → 7 вхождений

Left as byte-identical to `origin/cleanup/refactor`, each with reason:

- line 414 `Any` + `type: ignore[valid-type]`: the value is a runtime-built
  `tuple[Literal[*criterion_ids], ...]`. Every honest ≤100-column layout is
  blocked: an if/else re-layout makes mypy report "Cannot assign multiple
  types ... without an explicit type[...] annotation"; an `object` or
  `type[object]` annotation is the forbidden object-dodge; keeping the ternary
  with the ignore comment on one line does not fit in 100 columns; hoisting
  the Literal into its own variable would need a second type-ignore. So the
  line stays as in base.
- four `type: ignore[valid-type]` (lines 440, 445, 455, 703), live in both
  modes under `--warn-unused-ignores`: mypy
  rejects runtime variables / variadic `Literal[*ids]` in type-expression
  position passed to `create_model`; no static alternative exists.
- seven `audit-ok[field_default]` (lines 172, 173, 246, 775, 776, 777, 788):
  deliberate wire-contract defaults (absent claim-class keeps the pre-S1
  strict gate; `ToolCallRef`/`AgentRun` defaults keep old recipe receipts
  compatible); the `field_default` heuristic requires the marker on every
  class-level constant default, and removing a default would change the wire
  schema.

Checks: ruff check ok; ruff format --check — the same four pre-existing hunks
as base, the edited line is format-stable; mypy 1.19.1, both default and
`--check-untyped-defs`: error set base = head, and with
`--warn-unused-ignores` no unused ignores remain; lint-imports ok;
`python scripts/audit_heuristics.py --vs-main` ok;
`python scripts/gen_model_tables.py --check` ok; pytest — no new failures.
