cleanup(core-input-limits): precise types replace removable markers in input_limits.py

Behavior, defaults, error codes/messages and public API are unchanged; every
change is annotation-only except two `getattr` reads whose attribute is
guaranteed by the declared parameter type.

Removed (verified via CodeGraph callers/impact + LeanCTX anchored reads,
including tests and fakes):

- `bindings_have_null_digest` / `bindings_have_stale_digest_version`:
  `getattr(b, "digest", None)` → `b.digest`, `getattr(b, "digest_version", None)`
  → `b.digest_version`. In the non-dict branch `b` is an `InputBinding`
  (signature `list[InputBinding] | list[dict[str, Any]] | None`); `digest` and
  `digest_version` are declared pydantic fields (``domain.py``), so the
  attribute always exists and the `getattr` default is unreachable. Callers
  (only ``apply_input_drift``, plus one direct test passing dict rows) pass
  dicts or ``InputBinding`` instances only.
- `apply_input_drift`: `previous_bindings: list[Any] | None` →
  `list[InputBinding] | list[dict[str, Any]] | None` — exactly the union the
  two callees declare; the only typed caller (``attempts.start_attempt``)
  passes `list[Any] | None`, still assignable.
- `snapshot_bindings_at_p2` / `study_user_input_bindings`: `get_meta: Any`
  → `GetMeta` (``taint.GetMeta = Callable[[str], Any | None]``), the alias
  already used for the same artifact-store callable by ``taint.bindings_tainted``.

Left as in base, with reasons (line: marker: why):

- 85, 92: `Any` — pydantic ``mode="before"`` field validators receive
  arbitrary pre-validation input and return it; ``Any -> Any`` is the
  validator contract.
- 122: `Any` — value read from an untrusted client ``dict[str, Any]``.
- 160, 335: `dict[str, Any]` — public API accepts arbitrary client payloads.
- 195, 518: `dict[str, Any]` — pydantic ``model_dump()`` / ``model_copy(update=)``
  contract; heterogeneous values.
- 209: `getattr(profile, label)` — the attribute name is a loop variable over
  the five int-field labels; the attribute is guaranteed by the ``InputLimits``
  type, but direct access needs a literal name, and the only marker-free
  alternative is rebuilding the loop (a form change only to drop the marker).
- 241, 270, 363: `Any` / `list[Any]` — coercion/enforcement entry points
  genuinely accept heterogeneous user input; ``list`` invariance forbids
  narrowing without breaking callers.
- 430, 432, 468, 482: `dict[str, Any]` — public API; JCS rows are
  heterogeneous by construction.
- 535, 634, 637: `getattr(meta, "sha256"/"name", None)` — ``meta`` is
  duck-typed (attribute object, dict, or ``None``) per the ``GetMeta``
  contract; the default is reachable for dict-shaped metas, and narrowing the
  shape handling would change unexpected-input behavior.
- 589, 593, 609: `dict[str, dict[str, Any]]` / `list[Any]` — journal attempt
  rows are ``Any``-valued dicts; the bindings element read from them is
  genuinely ``Any``.

Counts before → after: `Any` 21 → 19 lines (dict-shaped contract unions
remain), `getattr` 6 → 4, `type: ignore` 0 → 0, `cast(` 0 → 0, `audit-ok`
0 → 0. `ruff format --check` on this file already fails on unchanged base
lines (multi-line call style); no base line was reformatted.
