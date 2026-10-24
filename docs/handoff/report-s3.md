# Handoff: CAPABILITIES #16 (report) + #17 (S3 / presigned)

Owner of **#16 report contract**: this agent. #17 is sequenced after #16 lands.
Base: `main` @ `2ac0d5b`. Branch: `cursor/capabilities-16-17-report-s3-8ae1`.

## Область

| # | Scope | Acceptance (CAPABILITIES / DESIGN) |
|---|---|---|
| **16** | Report as journal projection | JSON, MD, HTML with `artifact_id` links; НИР format; RunReport metrics: time_to_plan, successful-run share, valid-artifact share, tokens, cost |
| **17** | Artifacts in object storage | S3-compatible, env-driven endpoint/bucket/credentials (never printed); API returns / 302-presigned URLs; tests with **moto** |

Hard rules for follow-up PRs: **≤400 lines/PR**, **≤1 open PR**, stack **≤2**, **no shims**, no hardcodes/heuristics (config or typed refuse), markers hand-only, `scripts/audit_heuristics.py --vs-main` green.

**Reuse (mandatory for #16):** extend `coscientist/core/finalize/minimal_report.py` (`build_minimal_report`, `render_markdown_report`, sections real/stub/human) — do **not** invent a parallel report model. M5 formats and RunReport metrics layer on top of that projection; DESIGN §11 `coscientist/reporting/` is the packaging home once the first format lands.

## Consumes

| Piece | Owner | How #16/#17 use it |
|---|---|---|
| `minimal_report` / `render_markdown_report` / finalize payload | **core finalize** (on main) | Project only; never re-decide verdict/dispositions |
| `HypothesisVerdictRecorded` (+ audit→event path) | **research-cycle #51** (`RC12a`) | **Explicit dependency:** `#16b` waits for #51’s event on main (or stacked after it). Projection mapping **reads** journal rows; does **not** register the event type in core |
| `VerdictSnapshotsRecorded` / `StudyFinalized.hypothesis_verdicts` | **core claims / finalize** (on main) | Same — fold via projection handlers |
| `ArtifactRegistered.validity` | **core/graph** | Read-only for valid-artifact share; reporting does not invent or stamp `validity` |
| `UsageRecorded` (tokens/cost wire) | **planner** | `#16b` **depends on the planner’s PR**; production path uses real journal events once present. **Synthetic `UsageRecorded` rows only in tests** until that PR lands — reporting does not own or bump core `EventType` |
| `BudgetDebited` | **core / HITL** (on main) | Cost contribution when present in journal |
| Graph / CAS artifact blobs | **core artifacts** | #17 stores blobs; sha256/artifact_id from core |
| S3 credentials / endpoint | **env config** | Names in config; values never printed |

`EventMapping.register` = **report projection mapping only** (which journal `type` feeds which accumulator). It is **not** a second event-registration path and must not call `register_upcaster` / extend `EventType`.

## Состояние

- Main has finalize → `StudyFinalized` + `minimal_report` / markdown path; experimental HITL spend stub in `coscientist/experimental/report.py` (**deleted in `#16a`**).
- **No** `coscientist/reporting/` package, **no** S3 / presign API yet.
- `UsageRecorded` / `HypothesisVerdictRecorded` are **not** owned here (planner / #51).
- A first attempt at a full `reporting/` + S3 stack was **discarded** (≫400 lines, audit debt, shim risk). This handoff is the only deliverable of this turn.

## Сделано

- Branched from `2ac0d5b`.
- Read CAPABILITIES #16/#17, DESIGN §9–§11 / §10 RunReport, `minimal_report.py`, API surface, audit gate; aligned with #51 research-cycle plan.
- Scoped ownership: **#16 contract** owned here; #17 follows; consumes table above.
- Discarded oversized WIP; this document is the sole change.

## Следующий PR (small-PR plan, stack ≤2)

Each PR ≤400 lines (prod + tests + docs touch). One open at a time.

| Order | PR | Lines budget | Content |
|------:|---|---|---|
| **1** | `#16a` formats on `minimal_report` | ≤400 | `coscientist/reporting/`: thin wrappers — `render_json` / `render_html` / `render_nir` from **existing** `build_minimal_report` + `render_markdown_report` output; `artifact_id` links only where ids already appear in minimal_report / finalized payload; API `get_study_report(..., format=)` without new domain model. **Delete** `coscientist/experimental/report.py` (and retarget its tests to reporting or drop HITL-spend stub coverage into experimental HITL tests). Tests: synthetic finalized/`minimal_report` dicts → json/md/html/nir contain `artifact_id`. **No S3. No metrics.** |
| **2** | `#16b` RunReport metrics + hyp projection | ≤400 | **Depends on:** planner PR (`UsageRecorded` on main) **and** #51 `HypothesisVerdictRecorded` (`RC12a`) on main (or stack ≤2 on those heads). Metrics from journal timestamps/outcomes only (typed refuse if ts/outcome absent): `time_to_plan_s`, success share, valid-artifact share via **core/graph-owned** `ArtifactRegistered.validity` (absent → unassessed, share null — no inference). Tokens/cost: projection mapping over real `UsageRecorded` / `BudgetDebited`; **tests may use synthetic `UsageRecorded` rows only**. Hyp: projection handlers for `HypothesisVerdictRecorded` / `VerdictSnapshotsRecorded` / finalized `hypothesis_verdicts` via `EventMapping.register` (**projection only**). Tests per metric + mapping refuse. |
| **3** | `#17a` object-storage config + API shape | ≤400 | `ObjectStorageConfig` from env; secrets by **env var name**; typed `ObjectStorageConfigRefuse`; `presigned_get` contract + API `get_artifact_presigned_url` / 302. Exercise with **moto** (no alternate stub product). |
| **4** | `#17b` boto3 adapter | ≤400 | `s3_boto.py` only imports boto3/botocore (import-linter); moto integration; never print secrets. |

Stack: at most `#16a` ← `#16b` (or sequential merge); then `#17a` ← `#17b`. Do not open #17 until #16a is merged (stack depth ≤2). Do not open `#16b` until planner `UsageRecorded` and #51 `HypothesisVerdictRecorded` are available to depend on.

## Контракты

### #16 report (owned)

```text
project(journal_or_finalized) -> report_view
render(report_view, format ∈ {json, md, html, nir}) -> (body, media_type)
  | ReportFormatRefuse(format)
```

- **Source of truth for study narrative:** `minimal_report` / `StudyFinalized` from core finalize — reporting **projects**, does not re-decide verdict/dispositions.
- **Formats:** json = structured dump of that projection (+ metrics once `#16b`); md = existing `render_markdown_report` (+ additive metrics/NIR); html/nir = deterministic renders with `artifact:{id}` / `#artifact-{id}` links.
- **Metrics:** null + `*_unavailable` when inputs missing; never invent shares from media_type / name; `validity` only as stamped by core/graph.
- **Projection mapping:** `EventMapping.register` wires journal `type` → accumulator; builtin overwrite → `EventMappingConflict`; bad payload → `EventMappingRefuse`. **Not** core event registration.
- **import-linter:** `core` ↛ `reporting`; `reporting` ↛ `api`/`agents`/`tools`/`llm`/`mcp`/`web`; `api` may import `reporting`.

### #17 storage (after #16a)

```text
object_storage_config_from_environ(env) -> ObjectStorageConfig | ObjectStorageConfigRefuse
storage.put_bytes / presigned_get(key) -> PresignedGet | PresignRefuse
GET .../artifacts/{aid} -> 302 Location: presigned URL
```

- Credentials: config holds env **names**; `read_secret` at call time; `__repr__` shows names only.
- Key: `{prefix}artifacts/{artifact_id}/{sha256}` — refuse path separators in ids.
- Tests: **moto**.

## Вопросы

1. Should `#16a` take a finalized **payload** only, or also fold raw event lists? (Recommend: payload path in `#16a`; event fold in `#16b`.)
2. NIR: Markdown-only for M5, or also HTML? (Plan assumes MD НИР as `format=nir`.)

## Проверки

Per PR before push:

```bash
python scripts/audit_heuristics.py --vs-main
pytest -q tests/reporting -m "not slow"   # or targeted paths
pytest -q tests -m perf
lint-imports
```

Acceptance mapping:

- #16 formats + links + delete `experimental/report.py` → `#16a`
- #16 metrics + hyp projection (after planner + #51) → `#16b` (synthetic journals in tests only where noted)
- #17 config/presign + moto → `#17a`/`#17b`

## Риски

| Risk | Mitigation |
|---|---|
| Rebuilding report beside `minimal_report` | `#16a` calls into `minimal_report` / finalized payload; ban duplicate section logic |
| >400 line “full M5” PR | Slice table above; discard and re-handoff if exceeded |
| Heuristics audit on `or []` / field defaults | Required fields + explicit nulls; hand markers only with unique reasons |
| `#16b` blocked on planner / #51 | Do not invent `UsageRecorded` / `HypothesisVerdictRecorded` here; synthetic rows **tests only**; depend explicitly |
| Treating `EventMapping` as event registry | Document + review: projection mapping only; no `EventType` / upcaster changes in reporting PRs |
| Stamping `ArtifactRegistered.validity` in reporting | Owned by core/graph — read only |
| Secret leakage in logs/repr | Env names only; tests assert secret values absent from `repr` |
| Stack >2 / multiple open PRs | One PR open; next only after merge (or single stack parent) |
