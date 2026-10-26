# Scientific plan acceptance

Execution success and an answered research question are separate contracts.
PlanIntent now preserves the declared answer form and catalogue criterion ids.
Task `question_indices` refers to the 1-based positions in `questions`; an empty
list is allowed for preparation work but does not answer any question. Every
question needs an explicitly addressing task. Missing answer forms are refused
by the typed schema rather than becoming artifact questions.

For `verdict` and `both`, each addressing task needs a required substantive
catalogue check. Checks, thresholds, roles and freeze policy remain authored in
the catalogue; the planner selects references and cannot overwrite them.
An explicit `artifact` request can legitimately ask only for file delivery.
Artifact existence is never automatically promoted to a scientific verdict.

The generic `research_result_payload` laboratory template checks a coder JSON
result at `/results`. It is a validity requirement for an evidence-bearing
payload, not a test of scientific truth. Studies still need suitable authored
prediction/measurement checks and countable claims for scientific verdicts.
Tool-specific checks must name that tool's actual output; derivation rejects
references to unrelated output slots.

Artifact acceptance requires every required covering output rubric and every
selected required validity criterion on the current attempt. A successful
preparation task, an optional output or a previous attempt cannot conceal a
failed/missing required result. Optional task/output failures do not block the
required deliverable. Scientific prediction failure is not treated as validity
failure: a valid refutation remains a scientific result.

## Known boundaries

- `schema_valid` currently verifies artifact bytes/digest but does not execute
  JSON Schema validation. `source_link` is also an unconditional legacy check.
  Neither qualifies as a substantive scientific criterion in derivation.
- A JSON pointer proves the requested value exists; it does not prove its truth.
  Appropriate prediction checks, provenance and independent verification remain
  necessary. Selecting an inappropriate criterion cannot prove scientific merit.
- Supplied `--coder-jobs` programs and `--tool-call-args` are assisted integration
  inputs. Their successful execution does not establish autonomous code writing
  or autonomous argument selection. Benchmark reports must label that assistance.
- Existing journal verdicts are historical observations; this change does not
  rewrite previous `fulfilled` records or establish parity with legacy science.
