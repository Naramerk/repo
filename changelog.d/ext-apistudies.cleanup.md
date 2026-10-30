cleanup(ext-apistudies): replace 11 Any with dict[str, object] in `coscientist/api/studies.py`

Marker counts for `coscientist/api/studies.py`, by occurrences
(`grep -o | wc -l`; base = origin/cleanup/refactor):

- `Any`: 15 → 4
- `getattr`: 0 → 0, `type: ignore`: 0 → 0, `cast(`: 0 → 0,
  `audit-ok`: 0 → 0 (none present, none added)

Replaced (annotation-level, runtime-identical; `from __future__ import
annotations` keeps all function annotations as strings):

- 11x `dict[str, Any]` / `list[dict[str, Any]]` → `dict[str, object]` /
  `list[dict[str, object]]`: the `CreateStudyBody.inputs` pydantic field and
  ten parameter/return/local annotations of the JSON-payload helpers
  (`parse_create_study_body`, `_raw_body_digest`, `_raw_body_dict` param+
  return, `_catalog_version_from_raw`, `_request_from_raw_dict`,
  `_refuse_body_from_client` param+local, `_study_created_matches_body`,
  `use_inputs`). The `inputs` field verified equivalent
  at runtime: identical `model_json_schema()` properties, validate / dump /
  validate_json parity (incl. reject parity) on 6 probe payloads.
  Call chain checked with CodeGraph (`codegraph callers post_studies` —
  31 callers: tests + `api/__init__.py` re-export, no FastAPI route wraps
  it; `codegraph callers parse_create_study_body`) and LeanCTX signature
  reads: `LifecycleMixin.create_study` accepts
  `inputs: Sequence[UserInput | Mapping[str, object] | bytes | str]` and
  `input_limits`/`client_input_limits` as `Any | None`;
  `merge_input_limits(client: InputLimits | dict[str, Any] | None)` and
  `enforce_request_body_limit(raw: ... | dict[str, Any])` accept
  `dict[str, object]` (object is assignable to Any); `UnstampedEvent.payload`
  is `dict[str, Any]`. Mypy: full `coscientist`+`bench` scope clean;
  the test callers found by CodeGraph (8 files) give an error set identical to
  base in both default mypy and `--check-untyped-defs`.
- `_request_from_raw_dict`: result of `data.get("request")` bound to a local
  before the `isinstance(..., str)` check so mypy narrows the single lookup;
  with `dict[str, object]` the old two-lookup form (`data["request"].strip()`)
  loses the narrowing. Semantics identical: one `dict.get` call (no side
  effects), same isinstance filter, same `.strip()`, same None handling.

Kept as in base (no honest fix without behavior change, new cast/ignore,
or new audit-ok):

- `CreateStudyBody.input_limits: dict[str, Any] | None = None` — the audit
  `--vs-main` `field_default` heuristic flags ANY touched line carrying
  `= None`; new audit-ok is forbidden and splitting the annotation from the
  default is forbidden, so the line stays byte-identical to base.
- `post_studies` `body: ... | dict[str, Any] | ...`: rule 21. With
  `dict[str, object]`, tests/test_pr19_input_snapshot.py:1862/2312 pass a
  `dict[str, Sequence[Collection[str]]]` and get new `[arg-type]` errors
  (`dict` is invariant), so the parameter stays.
- `**create_kwargs: Any` in `post_studies` — open passthrough mirroring
  `create_study`'s heterogeneous kwargs (str/int/float/Decimal/datetime/
  SafetyPort/...); no precise type is provable, and `object` fails mypy at
  the `**create_kwargs` unpack into `rt.create_study(...)` (object is not
  assignable to `str`/`int`/...), which would force new cast/ignore —
  forbidden. The `Any` import stays for these annotations.
