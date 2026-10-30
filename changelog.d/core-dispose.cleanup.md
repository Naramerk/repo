claims/dispose: precise types instead of `Any` — no behavior change.

Counters (occurrences / вхождения, dispose.py): `Any` 10 → 3, `getattr` 9 → 9,
`cast(` / `type: ignore` / `audit-ok` 0 → 0.

Replaced (annotations only, in place; runtime-identical under
`from __future__ import annotations`):

- `validate_disposal`: `artifact_lookup` / `tasks` / `results` — `Any | None`
  replaced by `object | None`. `artifact_lookup` is `del`-ed unused (no caller
  passes it); `tasks` / `results` are opaque pass-throughs into
  `design_artifact_ok` (typed `Any | None` there); all sources (`dispose_question`
  getattr → `Any`, `executor.record` dict / `get_results()`, tests omit them)
  conform to `object`.
- `fold_question_disposed` store: `dict[str, dict[str, Any]]` replaced by
  `dict[str, dict[str, object]]` — rows are locally built records; callers
  pass `Any`-typed stores (isinstance-guarded getattr).
- `fold_question_dispose_rejected`: store/payload replaced by
  `list[dict[str, object]]` / `dict[str, object]` — body reads via `.get`,
  `str()`, `==` only; callers pass `EventEnvelope.payload` (`dict[str, Any]`)
  and `Any`-typed notes, which conform.
- `dispose_question`: dict arm `dict[str, Any]` replaced by
  `dict[str, object]` — unvalidated payload goes through
  `QuestionDisposal.model_validate`; the only caller (lifecycle) passes `Any`.

Kept, with reasons (line: marker: why):

- dispose.py:17 `Any` import: still required by :198 and :350.
- dispose.py:198 `payload: dict[str, Any]` (fold_question_disposed): body does
  `list(payload.get("refs") or [])`; with `object` this is a mypy error
  (`object` is not `Iterable`), and an isinstance restructure would add a
  behavior-affecting branch — forbidden; external journal payload stays `Any`.
- dispose.py:350 `runtime: Any` (_note_reject): lazily writes
  `runtime._disposal_rejects`, which is not declared on the `_DisposeRuntime`
  Protocol; typing the param makes that assignment a mypy error (no new
  ignore/cast allowed), and widening the Protocol would force minimal stub
  runtimes to carry the attr — a contract change.
- dispose.py:249,274,275,276,282,283,303,336,356 `getattr(runtime, ..., default)`:
  `study` / `plans` / `artifact_kinds` / `tasks` / `results` / `append_events` /
  `_disposal_rejects` are not declared on the intentionally minimal
  `_DisposeRuntime` Protocol (`clock` + `is_finalized` only), so conforming
  stub runtimes may lack them; defaults are load-bearing lazy init / feature
  detection — direct access would raise AttributeError (behavior change).
