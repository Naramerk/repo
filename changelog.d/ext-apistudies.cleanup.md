cleanup(ext-apistudies): replace 14 Any with dict[str, object] in `coscientist/api/studies.py`

Marker counts for `coscientist/api/studies.py`, by occurrences
(`grep -o | wc -l`; base = origin/cleanup/refactor 83f666db):

- `Any`: 15 → 2
- `getattr`: 0 → 0, `type: ignore`: 0 → 0, `cast(`: 0 → 0,
  `audit-ok`: 0 → 0 (none present, none added)

Replaced (annotation-level, runtime-identical; `from __future__ import
annotations` keeps all function annotations as strings):

- 14x `dict[str, Any]` / `list[dict[str, Any]]` → `dict[str, object]` /
  `list[dict[str, object]]`: two pydantic fields of `CreateStudyBody`
  (`inputs`, `input_limits`) and twelve parameter/return/local annotations
  of the JSON-payload helpers (`parse_create_study_body`, `_raw_body_digest`,
  `_raw_body_dict` param+return, `_catalog_version_from_raw`,
  `_request_from_raw_dict`, `_refuse_body_from_client` param+local,
  `_study_created_matches_body`, `post_studies` body, `use_inputs`).
  Pydantic fields verified equivalent at runtime: identical
  `model_json_schema()` properties, validate / dump / validate_json parity
  (incl. reject parity) on 6 probe payloads.
  Call chain checked with CodeGraph (`codegraph callers post_studies` —
  31 callers: tests + `api/__init__.py` re-export, no FastAPI route wraps
  it; `codegraph callers parse_create_study_body`) and LeanCTX signature
  reads: `LifecycleMixin.create_study` accepts
  `inputs: Sequence[UserInput | Mapping[str, object] | bytes | str]` and
  `input_limits`/`client_input_limits` as `Any | None`;
  `merge_input_limits(client: InputLimits | dict[str, Any] | None)` and
  `enforce_request_body_limit(raw: ... | dict[str, Any])` accept
  `dict[str, object]` (object is assignable to Any); `UnstampedEvent.payload`
  is `dict[str, Any]`. Mypy: full `coscientist`+`bench` scope clean
  (261 files); tests not in mypy CI scope re-checked on the 7 affected
  files: 104 errors before → 104 after (same sites, no new errors).
- `_request_from_raw_dict`: result of `data.get("request")` bound to a local
  before the `isinstance(..., str)` check so mypy narrows the single lookup;
  with `dict[str, object]` the old two-lookup form (`data["request"].strip()`)
  loses the narrowing. Semantics identical: one `dict.get` call (no side
  effects), same isinstance filter, same `.strip()`, same None handling.

Kept as in base (no honest fix without behavior change or new cast/ignore):

- `**create_kwargs: Any` in `post_studies` — open passthrough mirroring
  `create_study`'s heterogeneous kwargs (str/int/float/Decimal/datetime/
  SafetyPort/...); no precise type is provable, and `object` fails mypy at
  the `**create_kwargs` unpack into `rt.create_study(...)` (object is not
  assignable to `str`/`int`/...), which would force new cast/ignore —
  forbidden. The `Any` import stays for this one annotation.
