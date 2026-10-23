# Changelog

## Unreleased

### M1 PR5
- `InputSpec.mode` default is `forbid` (stubs only with an explicit mode); TaskCreated v3→v4 upcaster restores `use_partial` for old journals.
- `TaskSpec.tolerate_failed_dep` and H-c: FAILED/SKIPPED parents without `InputSpec` skip children as `input_unavailable`. Stage tasks (SPEC…REPORT) are excluded from H-c so a failed SPEC still allows PLAN/REPORT (DESIGN §7.5).
- `ExperimentMethod` enum matches graph schema; `method=simulation` is scientific metadata and does **not** upgrade stub/partial input provenance.

### M1 PR4 (API note)
- `coscientist.core.intake.intake` / `coscientist.core.reasons.intake` are **async** (`await intake(...)`). Sync call sites must use `asyncio.run` or an event loop. Classifier timeouts use `asyncio.wait_for(asyncio.to_thread(...))`.
