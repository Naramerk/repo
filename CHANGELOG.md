# Changelog

## Unreleased

### M1 PR5
- `InputSpec.mode` default is `forbid` (stubs only with an explicit mode).
- `TaskSpec.tolerate_failed_dep` and H-c: FAILED/SKIPPED parents without `InputSpec` skip children as `input_unavailable`.
- `experiment.method=simulation` (graph schema + `TaskSpec.method`) keeps attempt provenance `real`.

### M1 PR4 (API note)
- `coscientist.core.intake.intake` / `coscientist.core.reasons.intake` are **async** (`await intake(...)`). Sync call sites must use `asyncio.run` or an event loop. Classifier timeouts use `asyncio.wait_for(asyncio.to_thread(...))`.
