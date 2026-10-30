claims: precise types replace provably-removable Any in claims.py; one
`type: ignore[arg-type]` removed via a `Literal` annotation. Behavior and API
byte-identical (annotations are strings under `from __future__ import
annotations`; only test-side narrowing asserts added). Markers per line
(claims.py) — remaining ones kept deliberately:

Counters (occurrences / вхождения, claims.py): `Any` 24 → 7,
`type: ignore` 4 → 3, `getattr` 0 → 0, `cast(` 0 → 0, `audit-ok` 0 → 0.

Changed:
- 183 (`whitelist_claim_attrs`): `Mapping[str, Any] -> dict[str, Any]` →
  `Mapping[str, object] -> dict[str, object]`; body only copies whitelisted
  keys; sole caller (inputs.py `claim_nodes_from_attempt`) reads values via
  `str()`/truthiness and stores into `GraphNode.attrs: dict[str, Any]`.
- 205, 396, 715, 801, 937 (`results` params, 5 signatures): journal rows are
  untyped JSON payloads → `Mapping[str, Mapping[str, object]]`; bodies use
  values only via `str(...)`/membership. Callers pass `Any`-typed or
  covariant `Mapping` rows (inputs.py, dispositions.py, attempts.py, tests).
- 230, 243, 249 (`_coerce_str/_coerce_bool/_coerce_optional_int`):
  `val: Any` → `val: object`; bodies are isinstance guards + `str()` only.
- 262 (`_coerce_id_set`): `val: Any` → `val: Iterable[object] | None` — the
  expected input shape; str/bytes/bytearray/Mapping/scalar garbage is still
  rejected fail-closed at runtime by the unchanged isinstance/TypeError path.
- 393 (`runtime_claim_from_node`): `node: Any` → `node: GraphNode`; all
  callers pass `GraphStore.nodes_by_kind`/`get_node` nodes (CodeGraph
  callers/impact: claims.py + tests/test_pr8_verdict_runtime.py only); the
  two direct test call sites get narrowing `assert node is not None`
  (strengthened, not weakened).
- 398, 716 (`artifact_lookup`): bare `Any | None` → `Callable[[str], Any] |
  None` — the repo-wide shape (verdict.py). The meta payload itself stays
  `Any`: verdict.py declares `artifact_meta: Any | None` and tests pass
  dict-shaped metas, so `Callable[[str], Artifact | None]` (dispositions'
  variant) would misstate runtime values.
- 573+648: `target_kind` annotated `Literal["question", "hypothesis"]` →
  `# type: ignore[arg-type]` removed; same runtime values on every path.
- 805, 832, 864 (`compute_verdict_snapshots` payload) and 908, 910
  (`excluded_claims_report`): heterogeneous JSON payload dicts →
  `dict[str, object]`/`list[dict[str, object]]`; consumers are
  object-ready (attempts.py packs wholesale into bare `dict`;
  dispositions.py already reads snapshots as `Mapping[str, object]`).

Remaining markers, per line:
- 127 (`RuntimeClaim.effect: dict[str, Any] | None`): public dataclass field;
  occupied verdict.py/dispositions.py construct RuntimeClaim and pass the
  field into `Mapping[str, Any]` helpers; `dict` is invariant, so narrowing
  here risks mypy breakage in files this branch must not edit.
- 315, 349 (`# type: ignore[return-value]`): dead only because
  `GraphEdge.attrs` is `dict[str, Any]` (graph.py out of scope); removing an
  ignore that Any hides is forbidden by the task rules.
- 524 (local `effect: dict[str, Any] | None`): mirrors the exact return type
  of verdict.py `recompute_effect_provenance` — already precise.
- 859 (`# type: ignore[misc]`): removal would require re-typing the public
  `RuntimeClaim.provenance` field to the `Provenance` literal; the
  `in VALID_PROVENANCE` filter cannot narrow `str`.
- 940, 966 (`hypothesis_verdicts` payload): occupied dispositions.py
  `report_hypothesis_verdicts` feeds `snap.get("rule")` into
  `hypothesis_report_row(rule: str | None)`; narrowing to `object` would
  break mypy in a file this branch must not edit.

tests/test_pr8_verdict_runtime.py: +2 narrowing asserts (260, 2076); no test
semantics weakened.
