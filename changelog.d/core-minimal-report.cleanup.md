minimal_report: in value types of report input mappings and row dicts, replaced
`Any` with `object` (`Mapping[str, Mapping[str, object]]`,
`dict[str, object]`, …); the now-unused `typing.Any` import is dropped.
Annotation-only; no behavior change. `object` accepts every value that reaches
these functions, so no annotation is stricter than real sources.

Counters (occurrences / вхождения, coscientist/): `Any` −25, `getattr` 0,
`cast(` 0, `type: ignore` 0, `audit-ok` 0.

Kept byte-for-byte as base: the `truncate_minimal_report` loop with
`getattr(secs, key)` — removing it would need unrolling the loop, a form
change made only to drop the marker.
