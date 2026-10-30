coscientist/cli/run.py: fewer audit markers, no behavior change (occurrence
counts vs origin/cleanup/refactor: `Any` 5 to 1, `audit-ok` 8 to 6;
`getattr`/`type: ignore`/`cast(` 0 both sides).

- `_ctx_role_repair`: `type[Any]` replaced with the existing `_StageOut`
  TypeVar, so the repair callback is `Callable[[str], Awaitable[_StageOut]]`
  and the inner `_run` returns `_StageOut` instead of `Any`. Callers pass
  `ResearchSpec` and `HypothesisOut` (checked with
  `codegraph callers _ctx_role_repair`); the isinstance check against
  `output_cls` gives the compiler the exact type, and the TypeVar is erased
  at runtime. The one remaining `Any` occurrence is the English word "Any"
  inside a comment, not a type.
- The `remote_runtime` variable is now initialized in the branches that
  produce its value (`compose_remote_coder_runtime(...)` or `None`) instead
  of a pre-declared `X | None = None` line; this removed the
  `audit-ok[field_default]` marker. Same assignments on the same paths.
- `_prior_attempt_failure_reason` reads `row["role"]` directly instead of
  `row.get("role", "unknown")`, removing the `audit-ok[dict_get_default]`
  marker. The default was unreachable: every producer of `usages` rows sets
  `"role"` — `_usage_row` (coscientist/cli/study_helpers.py) always includes
  it, and all literal `usages.append({...})` sites in coscientist/cli/run.py
  plus `render_report_docs` in coscientist/cli/report_lane.py were checked
  the same way.

Call-chain verification commands: `codegraph callers _ctx_role_repair`,
`codegraph callers _stage_gate_review`, `codegraph callers
_prior_attempt_failure_reason`, `codegraph callers _usage_row`,
`codegraph callers _execute_executor_attempt`, `codegraph node
_usage_row`, `codegraph node render_report_docs`, plus `lean-ctx read`
anchored reads of the call sites.

Kept with justification: the six `audit-ok[except_silent_return]` markers
(two in the report gate lanes, four in the executor/hypothesizer attempt
paths) stay — the `return` inside those `except` blocks is the designed
typed close-and-stop flow (the attempt or gate barrier is closed via the
core before returning), and removing it would change HITL error handling;
that is follow-up material.
