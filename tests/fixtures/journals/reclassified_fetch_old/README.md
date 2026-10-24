# reclassified_fetch_old

Committed SQLite journal (`events.db`) in the **old** event schemas:

| type | schema_version |
|------|----------------|
| StudyCreated | 2 |
| PlanAccepted | 1 |
| TaskCreated | 2 |
| AttemptStarted | 1 (**no** `provenance` key) |
| AttemptTainted | 1 (`is_fetch=true`, tool=`papers_get`) |
| SourceFetched | 1 |

`AttemptStarted` predates required provenance. On open the real v9→v10
upcaster raises `CatalogError` / `AttemptStarted.v10:provenance_required`
(DESIGN_M1 §6.7 — never invent `real`). Covered by
`test_legacy_reclass_journal_refuses_missing_attempt_provenance`.

For open + reclassify finalize paths that need a journal fact
`provenance`, use sibling `reclassified_fetch_provenance`.

`reclassified_fetch_old.json` documents the same envelopes (source of the db).
