# reclassified_fetch_old

Committed SQLite journal (`events.db`) in the **old** event schemas:

| type | schema_version |
|------|----------------|
| StudyCreated | 2 |
| PlanAccepted | 1 |
| TaskCreated | 2 |
| AttemptStarted | 1 |
| AttemptTainted | 1 (`is_fetch=true`, tool=`papers_get`) |
| SourceFetched | 1 |

`reclassified_fetch_old.json` documents the same envelopes (source of the db).
Rebuild: edit the JSON, then regenerate `events.db` with the same columns /
`payload_digest` as `SqliteEventStore` (see prior agent scripts or
`coscientist.core.events.digest.payload_digest`).

`AttemptStarted` v1 payload still seeds required journal fields (`provenance`,
`charged`, `closed`, `attempt_role`) explicitly — core no longer invents them
on fold.

Used by `test_reclassified_tool_study_opens_finalizes_uncounted`: copy `events.db` into a temp study dir, open with live `papers_get` registered `is_fetch=False`, let upcasters run, finalize via `tick` past deadline.
