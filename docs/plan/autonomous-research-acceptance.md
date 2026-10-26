# Autonomous research: required outcome

The user supplies a research task, their data and optional constraints. After
one-time setup of trusted providers, credentials, MCP endpoints, permitted models,
filesystem/network permissions and hard spending limits, the system prepares and
runs the research without a per-task operator recipe.

Within those configured boundaries it must select roles, models and tools; decide
whether hypotheses/literature are needed; build a plan; allocate budgets; generate
and validate MCP arguments; generate and execute programs; evaluate observations;
and produce an evidence-backed report. It must not require hand-written
`tool_call_args`, `coder_jobs`, tool-name hints or a new profile for each task.

This does not authorize the system to mint credentials, enable new permissions,
add unapproved models/endpoints or increase spending ceilings. Missing user facts
require a concrete question or an explicit incomplete result, not invented inputs.
User approval policy remains separate from the researcher's reasoning.

## Ordered implementation and acceptance

1. Replace `schema_valid` and R13 `source_link` stubs with actual checks. Complete
   HYP → experiment → prediction/validity evaluation and replay. Empty, unrelated,
   forged or merely present artifacts cannot close a scientific question.
   Refutations and invalid executions remain distinguishable.
2. Expose a trusted capability catalog with actual MCP input schemas, model
   capabilities, cost/quota metadata and permissions. The core validates choices;
   selecting a tool/model does not grant new capabilities.
3. Replace static argument dispatch with a bounded decision/execution cycle over
   available tools and dependency evidence. It must work without a per-task
   argument file. Validate arguments against actual tool schemas before execution;
   record decisions, tool receipts, usage and remaining budgets.
4. Replace operator-authored coder declarations with generated programs and
   bounded environments. Preserve source, dependency versions, input hashes and
   structured outputs; reproduce a result from those records. Banner/stdout
   substring extraction is not a structured result protocol.
5. Choose HYP/LIT/check/report roles as part of the actual workflow. Distinguish
   publication metadata from accessed/parsed full text. Reports explain methods,
   limitations, contradictory observations and numbers sourced from Evidence.
   Reportability must not depend solely on an execution `fulfilled` flag.
6. Verify through the UI on new tasks without individual recipes. Expose the plan,
   limits, attempts, checks and sources. Exercise approval, Stop, retry/replan,
   restore and resizing. Compare actual scientific answers, methods, autonomy,
   cost and time with the legacy artifacts, not only status labels.

## Current state (2026-10-27)

This is the required target, not an implemented capability. Current presets pin
operator settings; MCP arguments and coder jobs are still operator-provided.
HYP/LIT Need selection exists but roles must be wired in advance; a complete
hypothesis-testing graph is absent. `schema_valid` and `source_link` still contain
the audited stubs. Historical 10/10 integration statuses are not scientific or
autonomy acceptance. Follow separate `lab/*` worktrees, independent exact-tip
reviews and required CI; preserve existing scientific/fail-closed gates.
