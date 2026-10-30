coscientist.core.finalize.minimal_report: journal-row parameters and local
report rows are typed with `object` value types instead of `Any`, the unused
`typing.Any` import is gone, and the sections-trimming loop uses direct
attribute access on the typed `MinimalReportSections` model instead of
`getattr`. Behavior, error codes, defaults and public signatures are
unchanged; all callers (finalize fold, tests) already pass compatible values.
