cleanup(ext-exphitl): replace expansion Any with object in hitl.py

`coscientist/experimental/hitl.py` (behaviour unchanged; API, codes, defaults,
logs intact):

- `_answer_proposed_check` parameter `expansion: dict[str, Any] | None`
  replaced with `dict[str, object] | None`: the method only reads string-ish
  values (`proposal_id`, `reviewer_task_id`) and its only caller is
  `answer_hitl` (CodeGraph: `codegraph callers _answer_proposed_check`;
  the `answer_hitl` callers in `coscientist/api/hitl.py` pass no expansion).
  mypy passes with no new ignores/casts; mypy over the hitl test files
  (`tests/experimental/test_hitl.py`, `tests/test_pr20_proposed_check.py`,
  `tests/test_pr12_budget_share.py`, `tests/test_pr9_plan_expanded.py`)
  (mypy 1.19.1, both default and `--check-untyped-defs`): error set base = head.

Marker occurrences in `coscientist/experimental/hitl.py`
(`grep -o '<marker>' file | wc -l`, base `origin/cleanup/refactor`):

- `Any`: 6 → 5 вхождений
- `getattr`: 8 → 8 вхождений
- `type: ignore`: 4 → 4 вхождения
- `cast(`: 0 → 0 вхождений
- `audit-ok`: 0 → 0 вхождений

Left as byte-identical to `origin/cleanup/refactor`, each with reason:

- 8 × `getattr`: an earlier attempt replaced the three `getattr(h, ...)`
  occurrences on declared `HitlRequest` fields (`payload_ref`,
  `expand_after_task`) with direct access — the fields are always present
  (`model_validate`-built instances, no `HitlRequest.model_construct`
  anywhere), so behaviour would be identical. Reverted: those lines are
  byte-identical to `origin/main`, and editing them makes their trailing
  `or ""` visible to `scripts/audit_heuristics.py --vs-main` as three new
  `[or_empty]` hits. Silencing them needs NEW `audit-ok` markers (forbidden)
  and removing/restructuring the `or ""` chains just so the heuristic stops
  seeing them is an audit bypass (owner rule 9/18), so the lines stay as in
  base. The five `getattr(self, ...)` calls are unchanged anyway: the mixin
  runs on a host class it does not declare — `_hitl_requests` /
  `_hitl_decisions` are created only in `core/execution/machine.py.__init__`,
  `_check_proposals` is not declared anywhere in the repo, so the defaults
  are reachable and direct access could raise `AttributeError`.
- 4 × `type: ignore` (`kind=kind` in `open_hitl`; `self` passed to
  `refuse_expansion` / `expand_plan`): `HitlRequest.kind` is the
  `HitlGateKind` literal while the public `open_hitl(kind: str)` signature
  must stay `str`; the mixin's `self` does not statically implement the host
  protocol of the core plan functions. None is dead (checked with
  `mypy --warn-unused-ignores`).
- 5 × `Any` (the `typing` import, `plans: dict[str, dict[str, Any]]`, and
  `expansion: dict[str, Any]` on `answer_hitl` / `_maybe_hitl_expand` /
  `_apply_budget_hitl`): `plans` mirrors the core machine's own annotation
  (`machine.py` `self.plans: dict[str, dict[str, Any]]`) and a wider value
  type would make `max(..., key=lambda p: p["revision"])` fail mypy without a
  cast; for `expansion`, `dict[str, object]` or a TypedDict creates new mypy
  errors at existing call sites in frozen test files (dict invariance, e.g.
  `exp: dict[str, str] | None` in `tests/test_pr20_proposed_check.py`) and
  turns `for t in (expansion.get("added_tasks") or [])` into an
  "object not iterable" error that would need a cast or a rewrite.
