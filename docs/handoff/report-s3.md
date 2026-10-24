# Handoff: CAPABILITIES #16 (report) + #17 (S3 / presigned)

**Owner (report #16/#17 track):** Ruslan And (`r.andreevich.s@gmail.com`) — cloud agent [Coscientist reports artifacts](https://cursor.com/agents/bc-d3f9dcc8-4204-5857-b9e9-3af108388ae1) (tasks `R16a`…`R17b` below).
**Owner ([#96](https://github.com/Naramerk/repo/issues/96) `HypothesisVerdictRecorded`):** Ruslan And — unlock via [#51](https://github.com/Naramerk/repo/pull/51) / [#87](https://github.com/Naramerk/repo/issues/87); report track does not invent the event type.
Base: `main` @ `b29d1a3`. Open plan-only PR: **#71** on branch `cursor/report-16a-formats-8ae1` (this tip).

## Область

| # | Scope | Acceptance (CAPABILITIES / DESIGN) |
|---|---|---|
| **16** | Report as journal projection | JSON, MD, HTML with `artifact_id` links; НИР format; RunReport metrics: time_to_plan, successful-run share, valid-artifact share, tokens, cost |
| **17** | Artifacts in object storage | S3-compatible, env-driven endpoint/bucket/credentials (never printed); API returns / 302-presigned URLs; tests with **moto** |

Hard rules for follow-up PRs: **≤400 lines/PR**, **≤1 open PR**, **no stacking while a prior PR is open** (next issue **Depends:** previous issue’s PR merged), **no shims**, no hardcodes/heuristics (**required config fields; otherwise typed refuse**), markers hand-only, `scripts/audit_heuristics.py --vs-main` green.

**Reuse (mandatory for #16):** extend `coscientist/core/finalize/minimal_report.py` (`build_minimal_report`, `render_markdown_report`, sections real/stub/human) — do **not** invent a parallel report model in `reporting/`. Typed `MinimalReport` lives in **core**. **One MD function:** `render_markdown_report` in that core file — `R16a` makes `MinimalReport` its public input; no second name (`render_markdown_from_minimal_report` forbidden). M5 formats and RunReport metrics layer on that projection. **`R16a.1` creates** DESIGN §11 package `coscientist/reporting/`.

## Consumes

| Piece | Owner | How #16/#17 use it |
|---|---|---|
| `minimal_report` / `render_markdown_report` / finalize payload | **core finalize** (on main; typed model in `R16a` / [#117](https://github.com/Naramerk/repo/issues/117)) | Project only; never re-decide verdict/dispositions |
| `HypothesisVerdictRecorded` (+ audit→event path) | **[#96](https://github.com/Naramerk/repo/issues/96)** — Ruslan And ([#51](https://github.com/Naramerk/repo/pull/51) / [#87](https://github.com/Naramerk/repo/issues/87)); #51 has only RC12 timeout→flag today | **Depends:** [#96](https://github.com/Naramerk/repo/issues/96). `R16b.1` ([#126](https://github.com/Naramerk/repo/issues/126)) projects journal rows; does **not** register the event type in core |
| `VerdictSnapshotsRecorded` / `StudyFinalized.hypothesis_verdicts` | **core claims / finalize** (on main) | Same — fold via projection handlers |
| `ArtifactRegistered.validity` | **core/graph** | Read-only for valid-artifact share; reporting does not invent or stamp `validity` |
| `UsageRecorded` (tokens/cost wire) | **[#97](https://github.com/Naramerk/repo/issues/97)** (scheduler S1; `#70` closed without `docs/plan/scheduler.md` on main) | `R16b` ([#125](https://github.com/Naramerk/repo/issues/125)) **depends on [#97](https://github.com/Naramerk/repo/issues/97)**. Production `R16b` projects real journal `UsageRecorded` only. **`R16b` unit tests require synthetic `UsageRecorded` fixtures** (production never invents the event type). Reporting does not own or bump core `EventType`. |
| `BudgetDebited` | **core / HITL** (on main) | Cost contribution from journaled `BudgetDebited`; if the event is absent → **null + `cost_unavailable`** (same metrics policy as DESIGN §10) |
| Graph / CAS artifact blobs | **core artifacts** | #17 stores blobs; sha256/artifact_id from core |
| S3 credentials / endpoint | **env config** | Names in config; values never printed |

`EventMapping.register` = **report projection mapping only** (which journal `type` feeds which accumulator). It is **not** a second event-registration path and must not call `register_upcaster` / extend `EventType`.

## Состояние

- Main has finalize → `StudyFinalized` + `minimal_report` / markdown path (dict today). HITL spend lives in `coscientist/experimental/report.py`; **`R16a.5` deletes that file** and moves the logic to `coscientist/reporting/hitl_spend.py` (DESIGN §11).
- **No** `coscientist/reporting/` package, **no** S3 / presign API yet.
- `UsageRecorded` / `HypothesisVerdictRecorded` are **not** owned here ([#97](https://github.com/Naramerk/repo/issues/97) / [#96](https://github.com/Naramerk/repo/issues/96)).
- Formats WIP that duplicated `MinimalReport` in `reporting/` was discarded; this handoff is the plan after review #71.
- `R16a` ([#117](https://github.com/Naramerk/repo/issues/117)) **Depends:** [#71](https://github.com/Naramerk/repo/pull/71) merged.

## Сделано

- Branched / rebased through `main` @ `b29d1a3` (#71 tip = this commit).
- Read [CAPABILITIES.md](../CAPABILITIES.md) #16/#17, DESIGN §9–§11 / §10 RunReport, DESIGN_M1 finalize, `minimal_report.py`, API surface, audit gate; deps tracked in [#96](https://github.com/Naramerk/repo/issues/96) / [#97](https://github.com/Naramerk/repo/issues/97).
- Scoped ownership: **#16/#17 contract** (`R16a`…`R17b`) — Ruslan And; [#96](https://github.com/Naramerk/repo/issues/96) — Ruslan And; consumes table above.
- Resliced after review #71 (discarded formats tip `3837c2d`, reachable in git history): core typed model before formats; sequential one-open queue (table below). Resolved Q1/Q2 (see § Решения).

## Следующий PR (small-PR plan — one open at a time)

Each PR ≤400 lines (prod + tests + docs touch). **One open PR at a time.** Each row **Depends:** previous row’s issue (merged PR). No parallel stack of `#71 ← R16a ← R16a.1`.

**Reslice after review #71 (discarded tip `3837c2d`):** a second `MinimalReport` pydantic copy in `reporting/` while `build_minimal_report` / `truncate_minimal_report` still return `dict` is not OK; substring “link” of markdown↔report is a heuristic. Core model + return-type change + finalize/tests + formats cannot fit one ≤400 PR with the formats package. Split:

| Order | Task ID | Issue | Owner | Lines | Content (one task) |
|------:|---|---|---|---|---|
| **0** | `R71` | **[#71](https://github.com/Naramerk/repo/pull/71)** (open, plan-only) | Ruslan And | docs | This handoff. |
| **1** | `R16a` | **[#117](https://github.com/Naramerk/repo/issues/117)** — **Depends:** [#71](https://github.com/Naramerk/repo/pull/71) | Ruslan And | ≤400 | Define pydantic `MinimalReport` (+ task/question/sections rows) in `coscientist/core/finalize/` (not reporting). **Typed row models + `extra=forbid` are mandatory**. `build_minimal_report` / `truncate_minimal_report` **return** `MinimalReport`. Wire for `StudyFinalized`: **`model_dump(mode="json")` only** (no `wire_dict`). Extend **`render_markdown_report`** so its public input is `MinimalReport` (same function in `minimal_report.py`; do not add a second MD renderer). Update core tests (`report["truncated"]` → attr; `model_dump` for jsonschema). **No** `coscientist/reporting/` yet. **No S3.** |
| **2** | `R16a.1` | **[#118](https://github.com/Naramerk/repo/issues/118)** — **Depends:** [#117](https://github.com/Naramerk/repo/issues/117) | Ruslan And | ≤400 | Create `coscientist/reporting/`: import `MinimalReport` from core. `ReportRenderInput` holds **report + artifact_id only** (no free markdown field); markdown via core **`render_markdown_report(report)`**. Formats `json`, `md`, `html` (`html` = same MD body wrapped). **No** code-span fix in this PR. |
| **3** | `R16a.1a` | **[#119](https://github.com/Naramerk/repo/issues/119)** — **Depends:** [#118](https://github.com/Naramerk/repo/issues/118) | Ruslan And | ≤400 | **Code-span bug** (seen on discarded #71 tips `f2f52b3` / `3837c2d`, still in git history): MD artifact links put the aid inside a code span ``[`aid`](href)`` but escaped `[` `]` `(` `)` with backslashes *inside* that span. Bug-locking test was `test_escape_aid_in_md_link`. Fix: no backslash escapes inside the code-span label; lock with `test_aid_in_code_span_no_backslash_escape`. URL path stays percent-encoded. |
| **4** | `R16a.2` | **[#120](https://github.com/Naramerk/repo/issues/120)** — **Depends:** [#119](https://github.com/Naramerk/repo/issues/119) | Ruslan And | ≤400 | One refuse path: `ReportInputRefuse` on direct construct **and** `parse_report_input`. `match=` on raises. No second ValidationError-only path. |
| **5** | `R16a.3` | **[#121](https://github.com/Naramerk/repo/issues/121)** — **Depends:** [#120](https://github.com/Naramerk/repo/issues/120) | Ruslan And | ≤400 | `format=nir` via `nir.py`: **body = core `render_markdown_report(MinimalReport)`**; `nir.py` adds only НИР framing (title from **config**, not hardcoded) — **no** own section/narrative logic. See § Решения Q2. |
| **6** | `R16a.4` | **[#122](https://github.com/Naramerk/repo/issues/122)** — **Depends:** [#121](https://github.com/Naramerk/repo/issues/121) | Ruslan And | ≤400 | Split multi-scenario format tests into one-assert-scenario-per-test; no bundled multi-path cases. |
| **7** | `R16a.5` | **[#123](https://github.com/Naramerk/repo/issues/123)** — **Depends:** [#122](https://github.com/Naramerk/repo/issues/122) | Ruslan And | ≤400 | **Delete** `coscientist/experimental/report.py`; move HITL-spend to `coscientist/reporting/hitl_spend.py` (listed in DESIGN §11). Behavior-preserving move only. |
| **8** | `R16a.6` | **[#124](https://github.com/Naramerk/repo/issues/124)** — **Depends:** [#123](https://github.com/Naramerk/repo/issues/123) | Ruslan And | ≤400 | Typed refuse for HITL-spend (`None→0.0`, `decided_by`/`approved_by`, non-dict payload). Tests. |
| **9** | `R16b` | **[#125](https://github.com/Naramerk/repo/issues/125)** — **Depends:** [#124](https://github.com/Naramerk/repo/issues/124), [#97](https://github.com/Naramerk/repo/issues/97) | Ruslan And | ≤400 | Adds RunReport **metrics** into the json dump only. Missing metric inputs → **null + `*_unavailable`** (DESIGN §10). Malformed mapped event payload → `EventMappingRefuse`. Metrics: `time_to_plan_s`, success share, valid-artifact share via **core/graph-owned** `ArtifactRegistered.validity` (absent → unassessed / null share). Tokens/cost: projection over real `UsageRecorded` / `BudgetDebited`. **`R16b` unit tests require synthetic `UsageRecorded` fixtures.** No hyp projection in this PR. |
| **10** | `R16b.1` | **[#126](https://github.com/Naramerk/repo/issues/126)** — **Depends:** [#125](https://github.com/Naramerk/repo/issues/125), [#96](https://github.com/Naramerk/repo/issues/96) | Ruslan And | ≤400 | Hyp projection only: handlers for `HypothesisVerdictRecorded` / `VerdictSnapshotsRecorded` / finalized `hypothesis_verdicts` via `EventMapping.register` (**projection only**). Tests per mapping refuse. See § Решения Q1. |
| **11** | `R17a` | **[#127](https://github.com/Naramerk/repo/issues/127)** — **Depends:** [#126](https://github.com/Naramerk/repo/issues/126) | Ruslan And | ≤400 | `ObjectStorageConfig` from env; secrets by **env var name**; typed `ObjectStorageConfigRefuse`; `presigned_get` contract; API `get_artifact_presigned_url` returns **302** Location. Exercise with **moto**. |
| **12** | `R17b` | **[#128](https://github.com/Naramerk/repo/issues/128)** — **Depends:** [#127](https://github.com/Naramerk/repo/issues/127) | Ruslan And | ≤400 | `s3_boto.py` only imports boto3/botocore (import-linter); moto integration; never print secrets. |

**Queue:** `#71` → [#117](https://github.com/Naramerk/repo/issues/117) → [#118](https://github.com/Naramerk/repo/issues/118) → [#119](https://github.com/Naramerk/repo/issues/119) → [#120](https://github.com/Naramerk/repo/issues/120) → [#121](https://github.com/Naramerk/repo/issues/121) → [#122](https://github.com/Naramerk/repo/issues/122) → [#123](https://github.com/Naramerk/repo/issues/123) → [#124](https://github.com/Naramerk/repo/issues/124) → [#125](https://github.com/Naramerk/repo/issues/125) (**Depends:** [#97](https://github.com/Naramerk/repo/issues/97)) → [#126](https://github.com/Naramerk/repo/issues/126) (**Depends:** [#96](https://github.com/Naramerk/repo/issues/96)) → [#127](https://github.com/Naramerk/repo/issues/127) → [#128](https://github.com/Naramerk/repo/issues/128).

## Контракты

### #16 report (owned)

```text
# Narrative formats (R16a.1 … R16a.3) — payload only (Q1):
project(StudyFinalized.minimal_report | MinimalReport) -> report_view
render(report_view, format ∈ {json, md, html, nir}) -> (body, media_type)
  | ReportFormatRefuse(format)

# Metrics (R16b) — journal events (Q1):
fold(journal events via EventMapping) -> RunReport metrics

# Hyp projection (R16b.1) — journal events (Q1):
fold(journal events via EventMapping) -> hyp rows
```

- **Source of truth for study narrative:** `MinimalReport` / `StudyFinalized` from core finalize — reporting **projects**, does not re-decide verdict/dispositions.
- **Formats (`R16a.1`/`R16a.3`):** json = structured dump of that projection; **`R16b` adds** metrics fields to that json; md = core `render_markdown_report(MinimalReport)` only (reporting has **no** `render_md.py`); html = `render_html.py` wraps that same MD with `artifact:{id}` and `#artifact-{id}` links; **nir:** `nir.py` takes that same body from `render_markdown_report(MinimalReport)` and adds only НИР framing (config title) — no second section renderer.
- **Metrics (policy):** missing inputs → **null + `*_unavailable`** (single rule; DESIGN §10). Never invent shares from media_type / name; `validity` only as stamped by core/graph. Malformed event payload → `EventMappingRefuse` (not used for mere absence).
- **Projection mapping (`R16b` / `R16b.1`):** `EventMapping.register` wires journal `type` → accumulator; builtin overwrite → `EventMappingConflict`; bad payload → `EventMappingRefuse`. **Not** core event registration.
- **import-linter:** `core` ↛ `reporting`; `reporting` ↛ `api`/`agents`/`tools`/`llm`/`mcp`/`web`; `api` may import `reporting`.

### #17 storage (**Depends:** [#126](https://github.com/Naramerk/repo/issues/126))

```text
object_storage_config_from_environ(env) -> ObjectStorageConfig | ObjectStorageConfigRefuse
storage.put_bytes / presigned_get(key) -> PresignedGet | PresignRefuse
GET .../artifacts/{aid} -> 302 Location: presigned URL
```

- Credentials: config holds env **names**; `read_secret` at call time; `__repr__` shows names only.
- Key: `{prefix}artifacts/{artifact_id}/{sha256}` — refuse path separators in ids.
- Tests: **moto**.

## Решения (бывшие вопросы)

### Q1 — finalized payload vs raw events

**Decision: payload-only for narrative formats (`R16a.1`…`R16a.3`); raw event fold in `R16b` (metrics) and `R16b.1` (hyp).**

Basis:

- [DESIGN_M1.md](../DESIGN_M1.md) finalize: `minimal_report` is written **inside** the `StudyFinalized` payload (JSON schema B3 / JCS); fallback path still carries that payload field — narrative report is a finalized projection, not a live journal fold.
- [DESIGN_M1.md](../DESIGN_M1.md) §1 **Инварианты** (I1, :154): `closed ⇔ ∃ StudyFinalized` — study report surface is for a closed/finalized study; do not cite DESIGN §9 for that biconditional.
- [DESIGN.md](../DESIGN.md) §10 `RunReport` / `UsageRecorded`: metrics (tokens, cost, durations, ТЗ shares) come from journal usage/tool events — that is `R16b`, not the format renderer.
- [CAPABILITIES.md](../CAPABILITIES.md) #16 acceptance is JSON/MD/HTML + НИР by capability ID; narrative formats project the finalized report payload (DESIGN_M1), not a dual journal fold.

Contract consequence: drop `project(journal_or_finalized)`; formats take `MinimalReport` / `StudyFinalized.minimal_report` only.

### Q2 — NIR Markdown vs NIR-HTML

**Decision: `format=nir` = Markdown НИР only for M5. No NIR-HTML.**

Basis:

- [CAPABILITIES.md](../CAPABILITIES.md) #16: “JSON, MD, HTML …; отчёт о НИР” — NIR is a **fourth** deliverable beside JSON/MD/HTML, not an HTML mode of NIR.
- [DESIGN.md](../DESIGN.md) §11 package layout: `reporting/report.py hitl_spend.py nir.py render_html.py` — no `render_md.py`; study-report MD is core `render_markdown_report` only. **Rule:** `nir.py` body = `render_markdown_report(MinimalReport)`; framing only (config title); no own section logic.
- [DESIGN.md](../DESIGN.md) §9 API: `format=md|html|json|nir`; M5 milestone ([DESIGN.md](../DESIGN.md) §13) lists “MD/HTML/JSON/НИР” as four. `format=nir` produces Markdown (`text/markdown`), not a second HTML renderer.
- DESIGN §11 file split (`nir.py` vs `render_html.py`) is the binding reference for NIR vs HTML (CAP “Старый код” paths are not required for this decision).

`format=html` = `render_html.py` wrap of the same core MD. `R16a.3` owns NIR framing only.

## Проверки (DoD — every implementation PR)

Before push, and again as merge gate against current `main`:

```bash
python scripts/audit_heuristics.py --vs-main
pytest -q <targeted paths> -m "not slow"   # core tests for R16a; tests/reporting for R16a.*
pytest -q tests -m perf                     # p99 ≤ 50ms (perf suite gate)
lint-imports
```

Per-PR DoD (all required):

1. **p99 ≤ 50ms** — perf marker suite green.
2. **CHANGELOG** entry linking [CAPABILITIES.md](../CAPABILITIES.md) #16/#17 and [DESIGN.md](../DESIGN.md) §9–§11 / §10 (by section ID, no fabricated CAP anchors).
3. **Green push** of the PR branch.
4. **Green merge-ref:** branch **rebased onto current `main` tip**; CI mergeable.

Acceptance mapping:

- #16 core typed `MinimalReport` (+ typed rows, `extra=forbid`) → `R16a` / [#117](https://github.com/Naramerk/repo/issues/117)
- #16 formats json/md/html → `R16a.1` / [#118](https://github.com/Naramerk/repo/issues/118)
- #16 code-span aid fix → `R16a.1a` / [#119](https://github.com/Naramerk/repo/issues/119)
- #16 single `ReportInputRefuse` path → `R16a.2` / [#120](https://github.com/Naramerk/repo/issues/120)
- #16 NIR `format=nir`: body from `render_markdown_report` + config title framing → `R16a.3` / [#121](https://github.com/Naramerk/repo/issues/121)
- #16 reporting test split → `R16a.4` / [#122](https://github.com/Naramerk/repo/issues/122)
- #16 delete `experimental/report.py` + HITL-spend move → `R16a.5` / [#123](https://github.com/Naramerk/repo/issues/123)
- #16 HITL-spend typed refuse → `R16a.6` / [#124](https://github.com/Naramerk/repo/issues/124)
- #16 RunReport metrics → `R16b` / [#125](https://github.com/Naramerk/repo/issues/125) (**Depends:** [#97](https://github.com/Naramerk/repo/issues/97))
- #16 hyp projection → `R16b.1` / [#126](https://github.com/Naramerk/repo/issues/126) (**Depends:** [#96](https://github.com/Naramerk/repo/issues/96))
- #17 config/presign + moto → `R17a` / [#127](https://github.com/Naramerk/repo/issues/127)
- #17 boto3 adapter → `R17b` / [#128](https://github.com/Naramerk/repo/issues/128)

## Риски

| Risk | Mitigation |
|---|---|
| Rebuilding report beside `minimal_report` | `R16a` owns typed model + `render_markdown_report(MinimalReport)` in core; `R16a.1`/`R16a.3`/`render_html.py`/`nir.py` call that one function for the body — ban a second MD section renderer in reporting |
| >400 line “full M5” PR | Slice table above; if a PR exceeds 400 lines, close it unmerged and open a smaller handoff PR before continuing |
| Heuristics audit on `or []` / field defaults | Required fields + explicit nulls; hand markers only with unique reasons |
| `R16b` / `R16b.1` **Depends:** [#97](https://github.com/Naramerk/repo/issues/97) / [#96](https://github.com/Naramerk/repo/issues/96) | Do not invent `UsageRecorded` / `HypothesisVerdictRecorded` here; `R16b` unit tests require synthetic `UsageRecorded` fixtures |
| Treating `EventMapping` as event registry | Document + review: projection mapping only; no `EventType` / upcaster changes in reporting PRs |
| Stamping `ArtifactRegistered.validity` in reporting | Owned by core/graph — read only |
| Secret leakage in logs/repr | Env names only; tests assert secret values absent from `repr` |
| Multiple open / deep stack | One PR open; next row **Depends:** previous issue merged — no `#71 ← R16a ← R16a.1` stack |
| Code-span aid escape regresses | Named bug + locking test in `R16a.1a` (`test_aid_in_code_span_no_backslash_escape`) |
