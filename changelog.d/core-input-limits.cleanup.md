cleanup(core-input-limits): `GetMeta` instead of `Any` for the artifact-meta callable in input_limits.py

Annotation-only change; behavior, defaults, error codes/messages and public
API are unchanged. No `getattr` removed.

Counters (occurrences / вхождения, coscientist/): `Any` −2, `getattr` 0,
`type: ignore` 0, `cast(` 0, `audit-ok` 0.

Changed:

- `snapshot_bindings_at_p2` / `study_user_input_bindings`: `get_meta: Any`
  → `GetMeta` (``taint.GetMeta = Callable[[str], Any | None]``), the alias
  already used for the same artifact-store callable by ``taint.bindings_tainted``.

Left as in base, with reasons (line: marker: why):

- 476, 491: `getattr(b, "digest"/"digest_version", None)` in
  `bindings_have_null_digest` / `bindings_have_stale_digest_version` — their
  only production caller is `apply_input_drift`, which passes
  `previous_bindings: list[Any] | None` (journal attempt rows), so `b` is
  duck-typed and attribute presence is not proven; kept byte-for-byte.
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
- 557: `previous_bindings: list[Any] | None` — journal attempt rows are
  ``Any``-valued; a precise union annotation would add a marker-bearing line
  absent from base (the newmarkers gate requires zero such lines), so the
  base annotation stays.
- 589, 593, 609: `dict[str, dict[str, Any]]` / `list[Any]` — journal attempt
  rows are ``Any``-valued dicts; the bindings element read from them is
  genuinely ``Any``.
