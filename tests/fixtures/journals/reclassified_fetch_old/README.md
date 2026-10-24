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

Used by `test_reclassified_tool_study_opens_finalizes_uncounted`: copy `events.db` into a temp study dir, open with live `papers_get` registered `is_fetch=False`, let upcasters run, finalize via `tick` past deadline.
