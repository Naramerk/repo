source_fetch: event batches typed `Sequence[EventEnvelope]` instead of
`list[Any]` (`apply_source_fetch_events`, `load_source_fetch_index`) and
`store_fetched_source` returns `Artifact` instead of `Any`. Annotation-only;
no behavior change.

Counters (occurrences / вхождения, coscientist/): `Any` −3, `getattr` 0,
`cast(` 0, `type: ignore` 0, `audit-ok` 0.

Kept byte-for-byte as base (rule 19/21: no annotation stricter than real sources):
- `getattr(writer, "store", None)`, `getattr(getattr(writer, "store", None),
  "study_id", None)`, `getattr(e, "type", None)`, `getattr(ev, "type", None)`:
  writers and event rows reach this module from several runtimes, executor
  wrappers and test fixtures; attribute presence is not proven for every
  source, so the defaults stay.
- `capability: Any` in `verify_fetch_capability` / `assert_fetch_capability_bound`
  and `capability: object` in `LocalArtifactStore`: these are validation
  entry points that must accept and reject arbitrary objects; a precise type would
  be stricter than what tests and callers pass.
