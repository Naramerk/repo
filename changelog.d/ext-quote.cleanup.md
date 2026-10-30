cleanup(ext-quote): precise meta/quote types in coscientist/checks/kinds/quote.py

Public API, behavior, error codes, defaults and logs unchanged.

All counts are OCCURRENCES (`grep -o <marker> | wc -l`) for
coscientist/checks/kinds/quote.py, before -> after:

- getattr: 6 -> 4. `getattr(meta, "producer", "agent")` and
  `getattr(meta, "tainted", False)` replaced with direct attribute access
  `meta.producer` / `bool(meta.tainted)`. `meta` comes only from
  `LocalArtifactStore.get_meta(...) -> Artifact | None`; `Artifact.producer`
  is declared with default `"agent"` and `Artifact.tainted: bool = False`,
  identical to the dropped getattr defaults, so no default branch was
  reachable for real `Artifact` instances. Call chain verified with CodeGraph
  (`codegraph callers _belongs_to_attempt`,
  `codegraph callers evaluate_quote_in_source`,
  `codegraph impact _belongs_to_attempt/_parse_source_quote`, `codegraph node
  get_meta/Artifact/ToolCallRef/ArtifactProducer/is_quote_source_producer/
  CheckResult/evaluate_code_check`) and LeanCTX anchored reads; no duck-typed
  sources exist — tests/checks/test_quote_in_source.py uses the real
  LocalArtifactStore (33 refs, no fakes), and no test imports the private
  helpers.
- `meta: Any | None` in `_belongs_to_attempt` replaced with `Artifact | None`
  (sole caller `_load_quotes` passes `artifacts.get_meta(...)`).
- `raw: Any` in `_parse_source_quote` replaced with `object` (the function
  runtime-narrows via `isinstance`; every JSON-derived caller conforms).

Kept markers (each with reason):

- 4 getattr occurrences: the `produced_by` / `call_id` chains in
  `_belongs_to_attempt` and in the `produced_by_call_id` effect field keep
  their base form byte-for-byte. Their `or ""` fallback is part of the base
  expression; direct access would expose that `or ""` as a changed line and
  fail `scripts/audit_heuristics.py --vs-main` (rule `or_empty`), while
  removing `or ""` just to satisfy the heuristic is a forbidden rewrite and
  new audit-ok markers are not allowed — so the base lines stay.
- 5 `Any` occurrences in code plus the `typing.Any` import (8 -> 6 total
  occurrences): the effect payloads stay `dict[str, Any]` because
  `CheckResult.effect` is typed `dict[str, Any] | None` in
  coscientist/core/execution/domain.py (outside this task's bounds) and a
  TypedDict is not assignable to `dict[str, Any]`; `prov_fields:
  dict[str, Any] | None` mirrors the shared cross-file check contract used by
  checks/evaluate.py, checks/kinds/executable.py and checks/kinds/_util.py and
  is deleted unused in the body.
- `type: ignore`: 0 -> 0, `cast(`: 0 -> 0, `audit-ok`: 0 -> 0.

Note: `ruff format --check` flags one pre-existing base line (the wrapped
`produced_by_call_id` expression, kept byte-identical per the rule above);
the same deviation exists in main 5909328 and origin/cleanup/refactor, and CI
runs `ruff check` only — every line changed by this PR is format-clean.
