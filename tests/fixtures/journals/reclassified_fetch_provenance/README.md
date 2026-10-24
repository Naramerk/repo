# reclassified_fetch_provenance

Committed SQLite journal (`events.db`) in the **old** event schemas, with
**explicit** `AttemptStarted.provenance` already present in the payload
(journal fact — not invented by an upcaster or temp-copy patch).

| type | schema_version |
|------|----------------|
| StudyCreated | 2 |
| PlanAccepted | 1 |
| TaskCreated | 2 |
| AttemptStarted | 1 (`provenance=real`) |
| AttemptTainted | 1 (`is_fetch=true`, tool=`papers_get`) |
| SourceFetched | 1 |

Sibling `reclassified_fetch_old` omits provenance and must raise
`AttemptStarted.v10:provenance_required` on open (DESIGN_M1 §6.7).

`reclassified_fetch_provenance.json` documents the same envelopes.
