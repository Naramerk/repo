plan_validate: direct access to declared pydantic fields instead of `getattr`
with a default (`OutputSpec.rubric/kind/role/evidence_kind`,
`QuestionSpec.parent_id/origin/acceptance_route`), `Catalog | None` for the
catalog parameters, `list[EventEnvelope]` for the refusal event batch,
`dict[str, object]` for raw question dicts.

Counters (occurrences / вхождения, coscientist/): `getattr` −13, `Any` −6.

Only functions whose inputs are `list[TaskSpec]` / `list[QuestionSpec]` were
touched; their callers (`LifecycleMixin.accept_plan`,
`expand_validate._validate`, both fed typed lists) were traced with CodeGraph,
and no `coscientist/tools/*` / `ctx.extra` path reaches this module.

Kept byte-for-byte as base:
- `apply_plan_refused`: `runtime: Any` and all its `getattr` (including
  `getattr(t, "state", None)` and `getattr(getattr(t, "spec", None),
  "task_key", None)` over `runtime.tasks`) — the runtime is duck-typed, so
  attribute presence is not proven.
- `_has_rubric` / `final_type` lines: they also carry `or`-defaults the
  vs-main audit would flag on any changed line; new audit-ok and
  `or`-rewrites are out of bounds.
