# Handoff: CAPABILITIES #16 (report) + #17 (S3 / presigned)

Owner of **#16 report contract**: this agent. #17 is sequenced after #16 lands.
Base: `main` @ `2ac0d5b`. Branch: `cursor/capabilities-16-17-report-s3-8ae1`.

## Область

| # | Scope | Acceptance (CAPABILITIES / DESIGN) |
|---|---|---|
| **16** | Report as journal projection | JSON, MD, HTML with `artifact_id` links; НИР format; RunReport metrics: time_to_plan, successful-run share, valid-artifact share, tokens, cost |
| **17** | Artifacts in object storage | S3-compatible, env-driven endpoint/bucket/credentials (never printed); API returns / 302-presigned URLs; tests with local fake (moto or stub) |

Hard rules for follow-up PRs: **≤400 lines/PR**, **≤1 open PR**, stack **≤2**, **no shims**, no hardcodes/heuristics (config or typed refuse), markers hand-only, `scripts/audit_heuristics.py --vs-main` green.

**Reuse (mandatory for #16):** extend `coscientist/core/finalize/minimal_report.py` (`build_minimal_report`, `render_markdown_report`, sections real/stub/human) — do **not** invent a parallel report model. M5 formats and RunReport metrics layer on top of that projection; DESIGN §11 `coscientist/reporting/` is the packaging home once the first format lands.

## Состояние

- Main has finalize → `StudyFinalized` + `minimal_report` / markdown path; experimental HITL spend stub in `coscientist/experimental/report.py`.
- **No** `coscientist/reporting/` package, **no** `UsageRecorded`/`ToolCalled` in `EventType`, **no** S3 / presign API yet.
- A first attempt at a full `reporting/` + S3 stack was **discarded** (≫400 lines, audit debt, shim risk). This handoff is the only deliverable of this turn.

## Сделано

- Branched from `2ac0d5b`.
- Read CAPABILITIES #16/#17, DESIGN §9–§11 / §10 RunReport, `minimal_report.py`, API surface, audit gate.
- Scoped ownership: **#16 contract** owned here; #17 follows.
- Discarded oversized WIP; this document is the sole change.

## Следующий PR (small-PR plan, stack ≤2)

Each PR ≤400 lines (prod + tests + docs touch). One open at a time.

| Order | PR | Lines budget | Content |
|------:|---|---|---|
| **1** | `#16a` formats on `minimal_report` | ≤400 | `coscientist/reporting/`: thin wrappers — `render_json` / `render_html` / `render_nir` from **existing** `build_minimal_report` + `render_markdown_report` output; `artifact_id` links only where ids already appear in minimal_report / finalized payload; API `get_study_report(events|payload, format=)` without new domain model. Tests: synthetic finalized/`minimal_report` dicts → json/md/html/nir contain `artifact_id`. **No S3.** |
| **2** | `#16b` RunReport metrics + hyp mapping | ≤400 | Metrics from journal timestamps/outcomes only (typed refuse if ts/outcome absent — no guesses): `time_to_plan_s`, success share, valid-artifact share **only** via explicit `validity` on `ArtifactRegistered` (or typed absent → unassessed, share null). Tokens/cost from extensible **typed** event mapping for `UsageRecorded` / `BudgetDebited` (synthetic journals; core EventType bump = separate PR if needed). Consume parallel-agent hyp/verdict via registerable handlers (`HypothesisVerdictRecorded` / `VerdictSnapshotsRecorded` / `StudyFinalized.hypothesis_verdicts`) — no overwrite of builtins. Tests per metric + mapping refuse. |
| **3** | `#17a` Fake object storage + config | ≤400 | `ObjectStorageConfig` from env (endpoint/bucket/region/key names/presign TTL/prefix); secrets by **env var name** only; typed `ObjectStorageConfigRefuse`; `FakeObjectStorage` + `presigned_get`; API `get_artifact_presigned_url` / 302 view. **No boto3 yet.** |
| **4** | `#17b` boto3 adapter + moto | ≤400 | `s3_boto.py` only imports boto3/botocore (import-linter); moto test; never print secrets. |

Stack: at most `#16a` ← `#16b` (or sequential merge); then `#17a` ← `#17b`. Do not open #17 until #16a is merged (or stacked as child of open #16 only if stack depth stays ≤2).

## Контракты

### #16 report (owned)

```text
project(journal_or_finalized) -> report_view
render(report_view, format ∈ {json, md, html, nir}) -> (body, media_type)
  | ReportFormatRefuse(format)
```

- **Source of truth for study narrative:** `minimal_report` / `StudyFinalized` fields already produced by core finalize — reporting **projects**, does not re-decide verdict/dispositions.
- **Formats:** json = structured dump of that projection + metrics; md = existing `render_markdown_report` (+ metrics/NIR sections as additive); html/nir = deterministic renders with `artifact:{id}` / `#artifact-{id}` links.
- **Metrics:** null + `*_unavailable` reason string when inputs missing; never invent shares from media_type / name heuristics.
- **Hyp/verdict:** typed extensible `EventMapping.register`; builtin overwrite → `EventMappingConflict`; bad payload → `EventMappingRefuse`.
- **import-linter:** `core` ↛ `reporting`; `reporting` ↛ `api`/`agents`/`tools`/`llm`/`mcp`/`web`; `api` may import `reporting`.

### #17 storage (next owner after #16a)

```text
object_storage_config_from_environ(env) -> ObjectStorageConfig | ObjectStorageConfigRefuse
storage.put_bytes / presigned_get(key) -> PresignedGet | PresignRefuse
GET .../artifacts/{aid} -> 302 Location: presigned URL
```

- Credentials: config holds env **names**; `read_secret` at call time; `__repr__` shows names only.
- Key: `{prefix}artifacts/{artifact_id}/{sha256}` — refuse path separators in ids.

## Вопросы

1. Should `#16a` take a finalized **payload** only, or also fold raw event lists? (Recommend: accept both — payload path for unit tests; event fold in `#16b`.)
2. Is `ArtifactRegistered.validity` the agreed wire field for “valid artifact share”, or will a parallel agent add `ArtifactAssessed`? (Until decided: explicit `validity` only; unassessed excluded from denominator.)
3. NIR: Markdown-only for M5, or also HTML? (Plan assumes MD НИР as `format=nir`.)

## Проверки

Per PR before push:

```bash
python scripts/audit_heuristics.py --vs-main
pytest -q tests/reporting -m "not slow"   # or targeted paths
pytest -q tests -m perf                   # once per landing PR if journal/write path untouched, still run once as required by task
lint-imports
```

Acceptance mapping:

- #16 formats + links → tests in `#16a`
- #16 metrics + hyp mapping → tests in `#16b` (synthetic journals)
- #17 fake/presign → `#17a`; moto → `#17b`

## Риски

| Risk | Mitigation |
|---|---|
| Rebuilding report beside `minimal_report` | `#16a` must call into `minimal_report` / finalized payload; ban duplicate section logic |
| >400 line “full M5” PR | Slice table above; discard and re-handoff if exceeded |
| Heuristics audit on `or []` / field defaults | Prefer required pydantic fields + explicit nulls; `Field(default_factory=…)` only when justified with unique hand markers |
| `UsageRecorded` not in core `EventType` | Mapping works on synthetic rows in `#16b`; core event registration = own PR if store must accept them |
| Secret leakage in logs/repr | Env names only; tests assert secret values absent from `repr` |
| Stack >2 / multiple open PRs | One PR open; next only after merge (or single stack parent) |
