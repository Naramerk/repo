# Graph area — PR plan

**Owner:** `coscientist/core/claims`
- Modules: `graph.py`, `graph_writer.py`, `graph_builder.py` (G10–G11, G13)
- Package: `coscientist/core/claims/graph_fold/` (G15–G16, G19–G20, G12)

**Consumers:** research-cycle (#51 RC9+), spec-lit. **Web M6** owns CAP #19 UI (not listed here).

**Also owns:** single journal write path for graph rows (fold/builder → G7 only; no duplicate `GraphNodeUpserted` vs finalize synthetic rows).

**Not owned here:** agent LLM/MCP impl, HITL engine ([#93](https://github.com/Naramerk/repo/issues/93) / CAP #18; not #52), web M6 UI, `ToolCalled` (unowned), `Mcp*Recorded` (unowned), `HypothesisVerdictRecorded` (no event-owner ID in #51 — see G12).

**Included:** execution/provenance kinds in schema contract (G14) and folds from owned source events — one graph, one write path.

Base: `main` @ `aa8be32`. **Contracts first:** G0 (this file / **#72**) before G7 (**#64**, stacks on schema parse **#73**).

**Rules:** ≤1 open PR; ≤400 lines/PR; stack ≤2; **no shims**; check `main` first; no hardcodes/heuristics; tests per new function.

**DoD (every PR):** before push — `audit_heuristics.py --vs-main`, size ≤400, rebase on tip `main`, CHANGELOG with CAP/DESIGN **anchors** + `[M1]`; read only own-module docs; run touched tests during work, full `pytest` + `pytest -m perf` (p99 ≤ 50 ms) once before push; `lint-imports`; **green CI**; short report (sha / fixed / checks). **Never merge from agent.**

**Parity:** [DESIGN §2](../DESIGN.md#2-доменная-модель) / [§16](../DESIGN.md#16-научная-модель-граф); [CAPABILITIES #19](../CAPABILITIES.md?plain=1#L28) (UI → M6).

**Gate:** **G12** blocked → unlock [#87](https://github.com/Naramerk/repo/issues/87). **G13** → [#67](https://github.com/Naramerk/repo/pull/67) G4. **G16** → [#129](https://github.com/Naramerk/repo/issues/129) S1+S2. **G17** blocked → unlock [#88](https://github.com/Naramerk/repo/issues/88). **G18** blocked → unlock [#89](https://github.com/Naramerk/repo/issues/89). **G19** → [#130](https://github.com/Naramerk/repo/issues/130) G6 + [#86](https://github.com/Naramerk/repo/issues/86). **G20** → [#90](https://github.com/Naramerk/repo/issues/90).

**G14 kind names (decided):** `llm_call`, `tool_call`, `mcp_exchange`, `job`, `plan_expansion` (+ provenance edges; schema version bump).

## PR list

| ID | Title | Scope | Owner | Depends |
|---|---|---|---|---|
| **G0** | docs: graph PR plan | This file + CHANGELOG. **#72**. | graph | `main` |
| **G7** | graph writer | `graph_node_upserted` / `graph_edge_added`; required registry; src/dst `GraphNode` pair-check; attrs vs schema fields; idem+content digest (`created_at` excluded). **#64** (stacks on **#73**). | graph | **G0**, **#73** |
| **G8** | migrate lifecycle graph writes | Replace direct graph writes in `plan/lifecycle.py` with G7. | graph + **core/plan** ([#84](https://github.com/Naramerk/repo/issues/84)) | G7, **#84** |
| **G8b** | migrate HITL graph writes | Replace direct graph writes in `experimental/hitl.py` (:529, :559) with G7. | HITL [#93](https://github.com/Naramerk/repo/issues/93) (CAP #18) | G7, **#93** |
| **G9** | migrate expand/contradicts/inputs | `expand_graph`, `contradicts`, `inputs` claim/experiment writers → G7. | graph | G7 |
| **G10** | Builder: ResearchSpecOut | study/question/criterion (+asks) via `graph_builder.py`. | graph | G7, **#85** (spec-lit `ResearchSpecOut`) |
| **G11** | Builder: LiteratureOut | paper/evidence/claim; **identity_key from SL6 only** (no DOI/URL heuristics; not #47). | graph | G7, **[#94](https://github.com/Naramerk/repo/issues/94)** (spec-lit SL6; #63 was SL1) |
| **G12** | fold `HypothesisVerdictRecorded` | **BLOCKED.** #51 `RC12` ≠ this event; `RC12a` only in `report-s3.md`. **Unlock:** [#87](https://github.com/Naramerk/repo/issues/87) (#51 adds event-owner ID). Scope: update existing hyp node attrs only. | graph | **#87** |
| **G13** | Builder: PlanOut / experiments | plan revision + experiment skeleton edges via `graph_builder.py`. | graph | G7, **#67 G4** |
| **G14a** | docs: DESIGN §16 counts | Update DESIGN §16 (+ M0 note) node/edge counts to match `graph_schema.yaml`. | graph | G7 |
| **G14** | schema: exec/provenance kinds | Decided kinds above + provenance edges; schema version bump. | graph | G7, **G14a** |
| **G15** | drop 2nd write path | Remove `graph_rows_from_finalized` use in `core/events/projections.py`; rows only via G7 + `graph_fold/`. | graph + **file owner `core/events`** (`projections.py`) | G7, G14 |
| **G16** | fold `UsageRecorded` | usage → `llm_call` nodes. Event: **scheduler S1** + **S2** ([#129](https://github.com/Naramerk/repo/issues/129); #70 closed unmerged). | graph | G15, **#129 S1+S2** |
| **G17** | fold `ToolCalled` | **BLOCKED — unowned.** tool-gap owns `ToolResolved` (G3/#65) + Job (G6) only. **Unlock:** [#88](https://github.com/Naramerk/repo/issues/88). | graph | **#88** |
| **G18** | fold `Mcp*Recorded` | **BLOCKED — unowned.** Not in #54 / main / DESIGN as journaled MCP req/resp. **Unlock:** [#89](https://github.com/Naramerk/repo/issues/89) (covers #78). | graph | **#89** |
| **G19** | fold `Job*` + `ArtifactRegistered` | Job: **tool-gap G6** ([#130](https://github.com/Naramerk/repo/issues/130); #46 closed unmerged); Artifact: **`core/execution`** ([#86](https://github.com/Naramerk/repo/issues/86)). Sandbox artifacts enter here. | graph | G15, **#130 G6**, **#86** |
| **G20** | fold plan-expansion | from `core/plan/expand*` → `plan_expansion` nodes/edges. | graph | G14, G15, **#90** |
| **G21** | node panel: node → its events | Query/API only: graph `node_id` → ordered source journal events that produced that node (and its edges). **Not a fold.** | graph | G15 |
| **G22** | material provenance + typed refuse | Path to roots; **typed refuse** on broken chain. G17/G18 out of scope. | graph | **G15 + G16 + G19** (all three) |
| **G23** | CAP #20 `NodeRequirement` on node | Attach catalog-metamodel `NodeRequirement` rows onto graph nodes (typed fields; refuse if metamodel missing/invalid). | graph | G15, **[#95](https://github.com/Naramerk/repo/issues/95)** (CAP #20 types) |
| **G24** | rebuild determinism | DESIGN §12.4: replay/rebuild ⇒ identical graph. | graph | G15 |

CAP #19 UI (former G25–G27) → **M6 web plan** (not this file).

## Event → G-PR → owner

Exactly **one** fold/builder G-PR and **one** event owner per row. `—` = not folded/built by graph area. Owners **call** the G7 writer (#64) without listing G7 in the G-PR column (G7 is the write API, not a per-event fold). Hyp **nodes** are written only by #51 RC9c–11c via G7 (no parallel hyp-node builder).

| Event | G-PR | Event owner |
|---|---|---|
| StudyCreated | G8 | plan/lifecycle |
| StudyRefused | — | plan/lifecycle |
| StudyCancelled | — | plan/lifecycle |
| StudyFinalized | G15 | finalize |
| TaskCreated | G20 | plan/machine |
| TaskInherited | — | machine |
| AttemptStarted | — | execution/machine |
| AttemptClosed | — | execution/machine |
| RouteReturned | — | execution |
| ResultRecorded | — | execution |
| TaskDone | — | execution |
| PlanAccepted | G13 | plan |
| PlanExpanded | G20 | plan/`expand*` |
| ExpansionRefused | G20 | plan |
| PlanRefused | — | plan |
| LoopCapped | — | plan/scheduler |
| LoopCompleted | — | plan/scheduler |
| LoopRefused | — | plan/scheduler |
| HitlOpened | G8b | HITL [#93](https://github.com/Naramerk/repo/issues/93) |
| HitlDecided | G8b | HITL [#93](https://github.com/Naramerk/repo/issues/93) |
| HitlTimedOut | G8b | HITL [#93](https://github.com/Naramerk/repo/issues/93) |
| HitlDeadlineExtended | G8b | HITL [#93](https://github.com/Naramerk/repo/issues/93) |
| BudgetDebited | — | scheduler |
| BudgetExceeded | — | scheduler |
| BudgetSharesAdjusted | — | scheduler |
| BudgetSharesRefused | — | scheduler |
| ArtifactRegistered | G19 | `core/execution` (#86) |
| LateWriteRejected | — | execution |
| DowngradeRejected | — | execution |
| CatalogErrorRaised | — | execution/checks |
| SandboxInfraRaised | — | execution |
| SameDigestRetryRejected | — | execution |
| LeaseAcquired | — | execution |
| ResultPending | — | execution |
| ReviewScheduled | — | checks/review |
| ReviewRecorded | — | checks/review |
| JudgeCasCorrupt | — | execution |
| ForkCreated | — | execution |
| VerdictSnapshotsRecorded | — | finalize (not a graph fold; G21 is query/API only) |
| GraphNodeUpserted | G7 | graph (#64 writer) |
| GraphEdgeAdded | G7 | graph (#64 writer) |
| EffectIntended | — | tools/execution |
| EffectDone | — | tools/execution |
| SideEffectDenied | — | tools/execution |
| SourceFetched | — | execution/lit |
| AttemptTainted | — | execution/lit |
| FetchIntegrityRaised | — | execution/lit |
| QuestionDisposed | G8 | plan/RC |
| QuestionDisposeRejected | G8 | plan/RC |
| CheckProposed | — | checks |
| CheckApproved | — | checks |
| CheckRejected | — | checks |
| UsageRecorded *(planned)* | G16 | scheduler **S1/S2** ([#129](https://github.com/Naramerk/repo/issues/129)) |
| ToolResolved *(planned)* | — | tool-gap **G3** (#65); not folded by graph area |
| ToolCalled *(planned)* | G17 | **unowned — blocked** ([#88](https://github.com/Naramerk/repo/issues/88)) |
| McpRequestRecorded / McpResponseRecorded *(planned)* | G18 | **unowned — blocked** ([#89](https://github.com/Naramerk/repo/issues/89)) |
| JobSubmitted / JobStatusChanged / JobTimedOut / JobCancelled / JobCancelFailed *(planned)* | G19 | tool-gap **G6** ([#130](https://github.com/Naramerk/repo/issues/130)) |
| HypothesisProposed *(planned)* | — (owner calls G7 #64) | **#51 RC9c** |
| HypothesisRefined *(planned)* | — (owner calls G7 #64) | **#51 RC10c** |
| HypothesisExperimentLinked *(planned)* | — (owner calls G7 #64) | **#51 RC11c** |
| HypothesisVerdictRecorded *(planned)* | G12 | **unowned — blocked** ([#87](https://github.com/Naramerk/repo/issues/87); `RC12a` only in `handoff/report-s3.md` → [#83](https://github.com/Naramerk/repo/issues/83)) |

## Notes

- Verdicts + HITL appear **in the graph**; HITL *engine* stays [#93](https://github.com/Naramerk/repo/issues/93) / CAP #18 (`experimental/hitl.py`).
- Spec-lit / RC **call** G7 (+ `graph_builder` where listed); they do not hand-roll envelopes.
- Empty `KindSpec.fields` forbids attrs at write; every YAML kind declares `fields` / `attrs`.
- Sandbox outputs that must appear in the graph arrive as `ArtifactRegistered` → **G19** (not a bare `—` sandbox row).

## Follow-ups (not in G7 schema/writer PRs)

Sites on `main` @ `aa8be32` (`coscientist/core/claims/graph.py`). Same line numbers as `0006805` for this file. Each row: **owner** + tracking **issue**.

| Item | File:line | Owner | Issue | Note |
|---|---|---|---|---|
| Non-strict `Frozen` | `graph.py:17-18` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | no `strict=True` |
| `KindSpec.layer = "research"` | `graph.py:23` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | require explicit layer in YAML |
| `KindSpec.schema_ref = None` | `graph.py:24` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Optional None default |
| `KindSpec.description = ""` | `graph.py:25` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Empty-string default |
| `KindSpec.prefix = None` | `graph.py:26` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Optional None default |
| `register_edge(pairs=None)` | `graph.py:43`, `:48-49` | graph | [#81](https://github.com/Naramerk/repo/issues/81) | Optional pairs; omit → no `_pairs` entry |
| `edge_allowed` fail-open | `graph.py:63-67` (`:65-66`) | graph | [#81](https://github.com/Naramerk/repo/issues/81) | require pairs; no second path |
| `GraphNode.parent_id` / `ref_id` None | `graph.py:87-88` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Optional None defaults |
| `GraphNode.attrs = {}` | `graph.py:89` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | `Field(default_factory=dict)` |
| `GraphNode.created_at = None` | `graph.py:90` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Optional default |
| `GraphEdge.attrs = {}` | `graph.py:99` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | `Field(default_factory=dict)` |
| `GraphEdge.created_at = None` | `graph.py:100` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | Optional default |
| `upsert_node` silent overwrite | `graph.py:111` | graph | [#80](https://github.com/Naramerk/repo/issues/80) | no prior-value check |
| `data.get("nodes"/"edges", [])` | `graph.py:156`, `:165` | graph | [#82](https://github.com/Naramerk/repo/issues/82) | Missing key → empty; no refuse |
| Literal `layer="research"` | `graph.py:160`, `:176` | graph | [#82](https://github.com/Naramerk/repo/issues/82) | Hardcoded in `registry_from_schema` |
| `text_ru` → `""` | `graph.py:161` | graph | [#82](https://github.com/Naramerk/repo/issues/82) | `str(n.get("text_ru", ""))` |
| `prefix=n.get("prefix")` | `graph.py:162` | graph | [#82](https://github.com/Naramerk/repo/issues/82) | Missing prefix → None; no refuse |
| `card` `""` + `str()` | `graph.py:166-168` | graph | [#82](https://github.com/Naramerk/repo/issues/82) | Missing/non-dict card coerced |
| `e.get("pairs") or []` | `graph.py:170` | graph | [#81](https://github.com/Naramerk/repo/issues/81) | Missing/empty pairs → `[]` |
| Legacy v0.2 `from`/`to` → cartesian | `graph.py:172-174` | graph | [#81](https://github.com/Naramerk/repo/issues/81) | **Shim — delete**; schema requires `pairs` |
| `pairs=pairs or None` | `graph.py:177` | graph | [#81](https://github.com/Naramerk/repo/issues/81) | Empty frozenset → fail-open |
| `report-s3.md` cites `#51 RC12a` | `docs/handoff/report-s3.md:22`, `:53` | report | [#83](https://github.com/Naramerk/repo/issues/83) | No such ID in #51; fix handoff (not in this PR) |
| `_SCHEMA_VERSION` literal | `graph.py` (after **#73**) | graph | [#115](https://github.com/Naramerk/repo/issues/115) | version string hardcode at load |
| `_card_desc` / `_edge_pairs` unknown keys; `out_by_src` unread | `graph.py` card/pairs helpers (after **#73**) | graph | [#91](https://github.com/Naramerk/repo/issues/91) | silent ignore; parse `out_by_src` |
| `_split_pipe` no paren depth | `graph.py` `_split_pipe` (after **#73**) | graph | [#91](https://github.com/Naramerk/repo/issues/91) | nested `()` + `\|` wrong |
| ad-hoc type parsers in tests | `tests/test_simulate_tolerate.py:98`, `tests/test_pr6_verdict_intent.py:227` | graph | [#92](https://github.com/Naramerk/repo/issues/92) | use `parse_attr_type` after #73 |
