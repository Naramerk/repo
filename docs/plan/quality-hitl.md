# Plan: #7 quality checks + #18 HITL engine

**Owner:** this agent owns the **HITL engine** (`experimental/hitl.py` hardened in place — no second path, no shims).
**Rules:** ≤1 open PR at a time; ≤400 changed lines/PR; stack depth ≤2; no shims; check `main` before branching; never merge.
**Base:** `main` (verify tip before each PR). Agent critic decisions use existing `AgentPort` (`coscientist.core.events.ports`) — **agent layer owns the impl; HITL waits for it; no CriticDecide stub/fake agent.**

Gates in scope (all driven by one engine + `HitlPolicy`/`GateConfig`, not per-gate code forks):
`spec_approval`, `plan` / `plan_approval`, `hypotheses`, `costly_or_nonreal`, `report`, `result_acceptance`, `skip_task`
(+ existing `budget` / `side_effect` / `proposed_check` stay on the same engine paths).

---

## Open now (PR #52)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q0** | CheckAttachment helpers (R23) — *this PR (#52)* | — (on `main`) | Pure `checks/attachment.py`: require ≥1 CodeCheck + ≥1 ReviewCheck; `unassessed_required_review_ids`; refuse UNASSESSED↔FAILED. Tests per function. |

---

## After #52 merges (ordered; open next only when previous is merged or stack≤2)

### Quality #7 (parallel-friendly with HITL only where Dep allows)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q1** | Wire `require_code_and_review` into `Executor.record` | **Q0** | Every `ResultRecorded`/`ResultPending` calls require; miss → `CatalogError`; both kinds kept in payload. |
| **Q2** | `ReviewScheduled` on every record exit | **Q1** | Success/pending/catalog_error/timeout exits that leave review UNASSESSED/PENDING emit `ReviewScheduled` (idempotent). |
| **Q3** | `Result.unassessed` fold uses attachment helper | **Q1** | Fold/`Result.unassessed` = `unassessed_required_review_ids` only (never FAILED ids). |
| **Q4** | `review_deadline` → UNASSESSED | **Q2** | Past review deadline journals UNASSESSED (not FAILED); outcome via `compute_outcome`. |
| **Q5** | Catalog defaults require code+review pair | **Q0** | `load_catalog` refuses experiment defaults lacking both kinds; fixture updated. |
| **Q6** | `compute_outcome` matrix tests only | **Q0** | B2b MET+UNASSESSED→SUCCESS; only-review UNASSESSED→PARTIAL; FAILED stays FAILED. |
| **Q7** | CAPABILITIES #7 acceptance suite | **Q1–Q6** | `test_cap7_*` 1:1 with #7 criteria. |

### HITL #18 — engine (this agent)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **G1** | GatePolicyOutcome pure map | **Q0** (indep. of Q1+) | Resolve timeout/headless/interactive outcome from `HitlPolicy.defaults` + `GateConfig` for **all** gates above; typed refuse if kind/policy missing. No guessed defaults in resolve. |
| **G5** | **HITL engine in `experimental/hitl.py`** (≤400 LOC) | **G1**; **AgentPort available** from agent layer (block if missing — do not stub) | **One** open/timeout/headless/answer path driven by GateConfig + G1 map for every gate (incl. `spec_approval`, `hypotheses`, `costly_or_nonreal`, `report`, `plan_approval`, `result_acceptance`, `skip_task`). Headless never hangs. Critic/delegate calls **injected `AgentPort`** only. Harden existing module in place — no parallel HITL package. Tests: headless + timeout matrix for ≥3 gates including `result_acceptance`/`skip_task` policy outcomes. |
| **G6** | HITL deadline extend + tick order | **G5** | `HitlDeadlineExtended` ≤ task/study; tick closes due gates before finalize. |
| **G7** | CAPABILITIES #18 acceptance suite | **G5–G6** | `test_cap18_*` 1:1 with #18. |
| **H1** | `docs/handoff/quality-hitl.md` | **G5** (after engine lands) | Handoff: what shipped, Deps left (Q*, AgentPort wiring), how to extend a gate via config only. Then **STOP**. |
| **C1** | CHANGELOG #7+#18 | **Q7**, **G7** | Changelog only; no behavior change. |

### Explicitly cancelled / folded

- ~~Per-gate open PRs (old 11–13)~~ → **G5**
- ~~Per-gate timeout PRs (old 14–16)~~ → **G5**
- ~~CriticDecide Protocol + fake~~ → use **`AgentPort`**; wait for agent layer; **no stub**
- ~~Second HITL path / new package~~ → harden **`experimental/hitl.py` only**

---

## Next action after this plan update

1. Land **Q0** (#52).
2. Open **G5** only after #52 merges **and** G1 is done (G1 may be folded into G5 if ≤400 LOC total — prefer one engine PR if G1+G5 fit).
3. Write **H1** handoff and **STOP** (quality Q1–Q7 may continue in later turns under ≤1 open PR).
