cleanup(ext-exphitl): direct HitlRequest attribute access, one Any replaced in hitl.py

`coscientist/experimental/hitl.py` (behaviour unchanged; API, codes, defaults,
logs intact):

- `getattr(h, "expand_after_task", None)` → `h.expand_after_task`,
  `getattr(h, "payload_ref", None)` → `h.payload_ref` (×2): both fields are
  declared on `HitlRequest` (`payload_ref: str` required, `expand_after_task:
  str | None = None`), every receiver is a real `HitlRequest` built via
  `model_validate`/`HitlOpened` projection (`HitlRequest.model_construct` is
  not used anywhere), so the `getattr` default is unreachable. Call chain
  checked with CodeGraph (`codegraph callers answer_hitl / _maybe_hitl_expand /
  _answer_proposed_check / _apply_budget_hitl / _hitl_timeout`,
  `codegraph query HitlForkMixin`) and LeanCTX reads.
- `_answer_proposed_check` parameter `expansion: dict[str, Any] | None`
  replaced with `dict[str, object] | None`: the method only reads string-ish
  values and its only caller is `answer_hitl` (mypy passes with no new
  ignores/casts; mypy over the hitl test files has the same 157 pre-existing
  errors before and after).

Marker occurrences in `coscientist/experimental/hitl.py`
(`grep -o '<marker>' file | wc -l`, verified against `~/zcode-cleanup/counts.sh`
repo-wide: no counter grows vs `origin/cleanup/refactor`):

- `Any`: 6 → 5 вхождения
- `getattr`: 8 → 5 вхождений
- `type: ignore`: 4 → 4 вхождения
- `cast(`: 0 → 0 вхождений
- `audit-ok`: 0 → 0 вхождений

Left as-is, each with reason:

- 5 × `getattr(self, ...)` (`_hitl_requests`, `_hitl_decisions`, `_check_proposals`):
  `HitlForkMixin` runs on a host class it does not declare; `_hitl_requests` /
  `_hitl_decisions` are created in `core/execution/machine.py.__init__` only,
  and `_check_proposals` is not declared anywhere in the repo, so the
  `getattr` defaults are reachable and direct access could raise
  `AttributeError` on an object without the attribute.
- 4 × `type: ignore` (`kind=kind` at `open_hitl`; `self` passed to
  `refuse_expansion` / `expand_plan`): `HitlRequest.kind` is the
  `HitlGateKind` literal while the public `open_hitl(kind: str)` signature must
  stay `str`; the mixin's `self` does not statically implement the host
  protocol of the core plan functions. None is dead (checked with
  `mypy --warn-unused-ignores`).
- 5 × `Any` (the `typing` import, `plans: dict[str, dict[str, Any]]`, and
  `expansion: dict[str, Any]` on `answer_hitl` / `_maybe_hitl_expand` /
  `_apply_budget_hitl`): `plans` mirrors the core machine's own annotation
  (`machine.py` `self.plans: dict[str, dict[str, Any]]`) and a wider value type
  would make `max(..., key=lambda p: p["revision"])` fail mypy without a cast;
  for `expansion`, `dict[str, object]` or a TypedDict creates new mypy errors
  at existing call sites in frozen test files (dict invariance, e.g.
  `exp: dict[str, str] | None` in `tests/test_pr20_proposed_check.py`) and
  turns `for t in (expansion.get("added_tasks") or [])` into an
  "object not iterable" error that would need a cast or a rewrite.
