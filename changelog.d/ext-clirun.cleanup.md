coscientist/cli/run.py: `Any` replaced by a TypeVar in `_ctx_role_repair`, no behavior
change (occurrence counts vs origin/cleanup/refactor: `Any` 5 to 1; `audit-ok` 8 to 8;
`getattr`/`type: ignore`/`cast(` 0 both sides).

- `_ctx_role_repair`: `type[Any]` replaced with the existing `_StageOut`
  TypeVar, so the repair callback is `Callable[[str], Awaitable[_StageOut]]`
  and the inner `_run` returns `_StageOut` instead of `Any`. Callers pass
  `ResearchSpec` and `HypothesisOut` (checked with
  `codegraph callers _ctx_role_repair`); the isinstance check against
  `output_cls` gives the compiler the exact type, and the TypeVar is erased
  at runtime. The one remaining `Any` occurrence is the English word "Any"
  inside a comment, not a type. The now-unused `Any` import is dropped.

Kept, byte-identical to base:
- `role = str(row.get("role", "unknown"))  # audit-ok[dict_get_default]` in
  `_prior_attempt_failure_reason`: replacing `.get` with `[]` changes behavior on
  unexpected input (the row type does not guarantee the key), so it needs a separate
  [behavior] PR.
- `remote_runtime: RemoteCoderRuntime | None = None  # audit-ok[field_default]`:
  moving the default into an `else:` branch would only hide the heuristic hit,
  not remove it.
- the six `audit-ok[except_silent_return]` markers (two in the report gate lanes,
  four in the executor/hypothesizer attempt paths): the `return` inside those
  `except` blocks is the designed typed close-and-stop flow; removing it would
  change HITL error handling (follow-up material).
