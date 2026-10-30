plan_validate: 17 of 19 `getattr` and 6 of 8 `Any` dropped without behavior
change — direct access to declared pydantic fields (`OutputSpec.rubric/kind/
role/evidence_kind`, `QuestionSpec.parent_id/origin/final_type/acceptance_route`,
`Task.state/spec.task_key`), `Catalog | None` for the catalog parameters,
`list[EventEnvelope]` for the refusal event batch, `dict[str, object]` for raw
question dicts. The remaining `runtime: Any` (+ its two `getattr`) stays: the
runtime crosses the nominal gap between `LifecycleMixin` (caller `self` in
lifecycle) and `StudyRuntime` (required by `finalize`), and both sides are
outside this PR's allowed files.
