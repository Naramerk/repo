loop: `capped_scope_questions(runtime)` annotated `StudyRuntime` instead of
`Any`. Annotation-only; no behavior change.

Counters (occurrences / вхождения, coscientist/): `Any` −1, `getattr` 0,
`cast(` 0, `type: ignore` 0, `audit-ok` 0.

Sources traced with CodeGraph: `StudyRuntime.view` (machine.py, `self`),
`finalize` (`runtime: StudyRuntime`) and tests that pass runtimes from
`open_runtime` / `runtime_cls`; no duck-typed fake reaches it.

Kept byte-for-byte as base (rule 19): `getattr(runtime, "plans", {})`,
`getattr(study, "max_tasks", 64)` and `getattr(runtime, "catalog", None)` —
`study` may be absent and attribute presence on every runtime source is not
proven, so the defaults stay.
