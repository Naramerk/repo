cleanup(ext-quote): precise meta/quote types in coscientist/checks/kinds/quote.py

Public API, behavior, error codes, defaults and logs unchanged.

Removed (occurrence counts for coscientist/checks/kinds/quote.py, before -> after,
verified with `grep -o <marker> | wc -l` and cross-checked against
`~/zcode-cleanup/counts.sh` per-file numbers):

- getattr: 6 -> 0. `getattr(meta, "produced_by", None)`,
  `getattr(produced, "call_id", "")`, `getattr(meta, "producer", "agent")`,
  `getattr(meta, "tainted", False)` and the nested
  `getattr(getattr(meta, "produced_by", None), "call_id", "")` replaced with
  direct attribute access. `meta` comes only from
  `LocalArtifactStore.get_meta(...) -> Artifact | None`; `Artifact.produced_by:
  ToolCallRef` and `ToolCallRef.call_id: str` are required declared fields,
  `Artifact.producer` is declared with default `"agent"` and
  `Artifact.tainted: bool = False` — identical to the dropped getattr defaults,
  so no default branch was reachable for real `Artifact` instances. Call chain
  verified with CodeGraph (`codegraph callers _belongs_to_attempt`,
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

- 5 `Any` occurrences in code plus the `typing.Any` import (8 -> 6 total
  occurrences): the effect payloads stay `dict[str, Any]` because
  `CheckResult.effect` is typed `dict[str, Any] | None` in
  coscientist/core/execution/domain.py (outside this task's bounds) and a
  TypedDict is not assignable to `dict[str, Any]`; `prov_fields:
  dict[str, Any] | None` mirrors the shared cross-file check contract used by
  checks/evaluate.py, checks/kinds/executable.py and checks/kinds/_util.py and
  is deleted unused in the body.
- `type: ignore`: 0 -> 0, `cast(`: 0 -> 0, `audit-ok`: 0 -> 0.
