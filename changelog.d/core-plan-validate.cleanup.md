plan_validate: 15 of 19 `getattr` and 6 of 8 `Any` dropped without behavior
change — direct access to declared pydantic fields (`OutputSpec.rubric/kind/
role/evidence_kind`, `QuestionSpec.parent_id/origin/acceptance_route`,
`Task.state/spec.task_key`), `Catalog | None` for the catalog parameters,
`list[EventEnvelope]` for the refusal event batch, `dict[str, object]` for raw
question dicts. Kept: `runtime: Any` (+ its two `getattr`) — the runtime
crosses the nominal gap between `LifecycleMixin` (caller `self` in lifecycle)
and `StudyRuntime` (required by `finalize`), both outside this PR's files;
`_has_rubric` / `final_type` lines keep their base `getattr` byte-for-byte —
the lines also carry `or`-defaults the vs-main audit would flag on any changed
line, and both new audit-ok and `or`-rewrites are out of bounds.
