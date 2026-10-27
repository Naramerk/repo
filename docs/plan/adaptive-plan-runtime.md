# Adaptive plan runtime — implemented slice and durable-layer boundary

Branch `lab/adaptive-plan-p1` (base `4cab544`). This document describes what the
branch actually implements, the review-blocker closures it carries over from the
parked `lab/adaptive-plan` iteration, and the honest boundary of the durable
layer that is **not** implemented here.

## Implemented in this branch

### 1. Expanded tasks are really dispatched (no second runner)

`cli/run.py::_drive_plan_attempts` keeps its first pass over the accepted
plan's drafts (unchanged behavior, plan order), and adds a second pass over
tasks that exist only in the journal fold — `PLAN-n` replan containers, tasks
added by an applied `PlanExpanded`, foreach materializations. Both passes share
one budget, one deadline, one lease, one tick loop:

* first attempts only for `iter_dispatchable_tasks()` tasks with no attempts;
* core-fired retries execute exactly once per driver (`executed_aids`);
* everything else stays with the core tick (deps, retry backoff, scheduling);
* dispatch state is read from the runtime fold, never from a second engine, so
  live execution and replay offer the same dispatch sequence.

`PLAN-n` containers are executed through the ordinary executor route: the
executor output schema (`ExecutorRouteOut`) gained an optional typed
`expansion_request` (the existing core `ExpansionRequest` type), and
`cli/journal.py::record_executor_route` passes it into `AgentTaskOutput`. The
deterministic critic gate (`expand_critic.critic_gate`) remains the only path
from a request to `PlanExpanded` — the CLI never applies an expansion itself
and never re-runs the planner for PLAN-n (no recursive `PLAN-n`: a PLAN-n task
is applied, never re-triggered).

### 2. Prior review blockers closed in the new implementation

The parked `lab/adaptive-plan` iteration (`e726883`, NOT merged) received three
independent review blockers. This branch closes them for the new runtime:

1. **P1 deadline at preseal/replay** — the run loop now evaluates the shared
   `StudyRuntime.dispatch_preflight()` before every dispatch pass. It uses the
   SAME trigger priority the tick seals with (`_terminal_trigger`:
   cancelled > budget > intake > deadline > all_done, extracted from `tick` so
   the two cannot drift). After the effective deadline no new attempt starts,
   and the seal keeps the authoritative deadline cause — a driver can no longer
   start post-deadline work, nor can an `all_done` seal hide a deadline.
2. **P1 real money budget at preseal** — `dispatch_preflight` runs the existing
   monetary producer (`_emit_study_budget_if_exhausted`: folded
   `BudgetDebited.spent_rub` totals → `BudgetExceeded`) and then reads the
   shared trigger state. Genuine funded exhaustion from debits alone flips the
   driver to the budget seal; there is no second budget evaluator anywhere in
   the driver. Expanded/PLAN-n work draws the same session `Budget`
   (`max_llm_calls`/`max_tokens`/`max_tool_calls`/deadline) as initial work —
   regression-tested at the CLI level.
3. **P2 exact no-op retries** — the store's I3 gate already implements the
   required semantics (identical `idem_key`+payload retry after
   `StudyFinalized` returns the original row / raises `Idempotent(orig_seq)`;
   altered same-idem payload → `Conflict`; new scientific write →
   `StudyClosedError`). The new driver adds no guard that would break this, and
   regression tests now pin all three behaviors post-seal.

### 3. Honest refusals on the expanded path

* An expanded task with `tools_hint=coder` and **no exact `--coder-jobs`
  binding for its task key** refuses typed (rc=3, "no positional job reuse").
  Original jobs are never reused positionally for new tasks.
* A runtime task whose spec carries core-owned fields the executor route
  cannot carry (`foreach` template, loop body, budget/deadline shares,
  method, inputs) refuses via `spec_redrivable` (same gate as continue).
* Stage placeholders (SPEC/PLAN/HYP/LIT/REPORT) pending in the run loop
  refuse typed instead of being silently re-run; DONE stage containers are
  skipped (their attempt rows may legitimately stay open — `accept_plan`
  closes the task, not the attempt).

## Review record (delta)

Independent delta review of the dispatch slice returned **review-ready k=1**
with one P2 blocker. The field-level fix below is correct and strictly better
than a defaulted field, but the strict-closed claim holds only at the TOP
level of the schema — recorded honestly:

* **P2 — closed top-level only; nested graph is a known live limitation.**
  The defaulted `ExecutorRouteOut.expansion_request` left the property
  outside `required` in `model_json_schema`. It is now required-nullable
  without a default (every constructor passes `expansion_request`
  explicitly; `None` for ordinary attempts), and
  `test_executor_route_out_schema_is_strict_closed` pins the top-level shape
  (all properties ∈ `required`, nullable via `anyOf[…, null]`).
  HOWEVER: OpenAI strict structured outputs apply recursively at every
  object level, and the `$ref ExpansionRequest` pulls in a nested graph
  (`ExpansionRequest` → `TaskSpec` → `OutputSpec`/`Rubric`/`RequirementRef`/…)
  whose `$defs` have properties outside `required`. A strict provider will
  therefore STILL reject the executor-route `response_format` — the same
  observable consequence as the original P2; relative to `2e6363d` (3
  required properties, single all-required `$def ToolArg`) the response
  schema regressed for strict providers. Mock-verified only. Recursive
  strictification / a strict-schema sanitizer (openai_compat boundary) is a
  separate slice and a prerequisite before any live strict provider.

  **Recursive-strict inventory for the sanitizer slice** (measured on this
  branch; both structured calls use `strict=True` response formats at
  `agents/adapter.py`):

  | response_format | schema | top-level | `$defs` not-all-required | strict-valid |
  |---|---|---|---|---|
  | `executor_route` | `ExecutorRouteOut` | 4/4 required | **9 of 13** (`ExpansionRequest` 5 optional props, `TaskSpec` 21, `OutputSpec` 6, `InputSpec` 7, `Rubric` 4, `RubricItem` 3, `QuestionSpec` 3, `RequirementRef` 3, `ForEachSpec` 1) | **no** |
  | `remote_coder_task` | `RemoteCoderTaskSpec` | 2/2 required | 0 of 1 (`RemoteCoderOutputSpec` all-required) | **yes** |

  So the sanitizer's target is the `executor_route` nested graph;
  `remote_coder_task` is already fully strict-valid and the recursive
  strictification must not regress it (the negative pins in
  `test_executor_route_out_schema_is_strict_closed` guard the gap until the
  sanitizer flips them).

Four further findings were returned at severity P3 (non-blocking), transcribed
from the review record:

* **P3-1 (coordinator's live-verification checklist)** — `expansion_request`
  is visible to the model in the output schema of EVERY ordinary
  executor-route call (an affordance). Misuse is bounded by the design gates
  (`max_expansions`, `replan_reserve`, the deterministic `critic_gate`), so it
  cannot spend or apply anything on its own — but flag it during live
  verification (does the ordinary executor ever emit a request unprompted,
  and does the gate absorb it).
* **P3-2** — `run.py` `_bind_coder_jobs` (~1264–1283): a pre-declared
  `--coder-jobs` key intended for a *predictable expanded task* (e.g. EXP-1)
  can be consumed by the positional equal-count binding of a plan task, so the
  expanded task then hits the rc=3 no-positional-reuse refusal. Fail-closed,
  but unexpected for an operator who declared the key up front. Accepted for
  this slice; the workarounds are exact keys for plan tasks or no positional
  fallback.
* **P3-3** — `continue_cmd.py` `_drive_round` (~476–484) lacks the
  `dispatch_preflight` gate: a journal reopened past the effective deadline
  can start an attempt that P5/tick then closes. Pre-existing behavior (not
  introduced by this slice); take the shared preflight into stage-2.
* **P3-4** — `run.py` runtime pass (~2612) imports `draft_from_task_spec` /
  `spec_redrivable` at function level while `continue_cmd` imports `run` at
  module level — a working cycle, but a shared CLI helper module would be
  cleaner. Accepted for this slice.

## Not implemented here — durable layer boundary

The following stays **out** of this slice and must not be advertised as done:

* **Durable LLM budgets.** Cumulative usage still lives in the in-process
  adapter (`agents/adapter.py`); `continue` opens a fresh adapter and the
  session budget restarts. Crash-safe budgets need a single durable
  pre-call reservation / post-call settlement typed entry on the existing
  `BudgetDebited` pipeline at the central provider boundary (design in
  `.worktrees/adaptive-plan/docs/plan/adaptive-planning.md` §"One resource
  boundary"): reserve fenced before the provider await, settle from the CAS
  persisted typed response, orphan reservations fail closed on resume.
  Reconciliation is a separate trusted operation requiring an actual provider
  receipt.
* **Exactly-once external calls are NOT promised.** A crash between reserve
  and settlement leaves an uncertain already-possibly-paid call; it is never
  automatically retried, refunded, or fabricated. Without provider
  idempotency keys this is a hard external constraint, not an implementation
  gap.
* **Durable keep/trigger/revision across restart.** `KEEP`-style planner
  revisions, the frozen context receipt, and trigger fold dedup survive
  restart today only via the existing idempotent events
  (`TaskCreated(PLAN-n)` deterministic idem, `PlanExpanded`/`ExpansionRefused`
  folds, `resume_pending_expansions` on reopen). A durable planner-revision
  ledger (single-successor grants, lineage-wide accounts, delegation/import
  before finalization) is stage-2+ work and a prerequisite for advertising
  global budgets.
* **Coder/tool argument generation.** Expanded coder tasks need explicit
  declared bindings; this slice generates no code, no argv, no tool arguments.

## Verification status

Mock-verified only: cassette LLM, no live models, no MCP, no scientific runs.
Gates for this slice: targeted CLI/core suites green, full
`pytest -q -m "not slow"` at the established macOS sandbox baseline, ruff,
mypy, import contracts, postcommit heuristics audit — see the branch return
packet for exact numbers. Per AGENTS.md, green CI is not research readiness.
