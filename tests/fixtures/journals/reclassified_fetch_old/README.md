# reclassified_fetch_old

Committed SQLite journal (`events.db`) at **schema v1** (store_format=1):

| type | schema_version |
|------|----------------|
| StudyCreated | 1 |
| PlanAccepted | 1 |
| TaskCreated | 1 |
| AttemptStarted | 1 |
| AttemptTainted | 1 (`is_fetch=true`, tool=`papers_get`) |
| SourceFetched | 1 |

`../reclassified_fetch_old.json` is the source of truth. Rebuild the DB with:

```bash
python scripts/gen_reclassified_fetch_old_journal.py
```

Used by `test_reclassified_tool_study_opens_finalizes_uncounted`: copy `events.db` into a temp study dir, open with live `papers_get` registered `is_fetch=False`, finalize via `tick` past deadline. Payloads are complete v1 (no upcast fills).
