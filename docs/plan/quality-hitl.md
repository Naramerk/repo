# Plan: #7 quality checks + #18 HITL engine

**Owner:** this agent owns the **HITL engine** (`experimental/hitl.py` hardened in place — no second path, no shims).
**Rules:** ≤1 open PR at a time; ≤400 changed lines/PR; stack depth ≤2; no shims; check `main` before branching; never merge.
**Base:** `main` (verify tip before each PR). Critic/delegate uses existing `AgentPort` (`coscientist.core.events.ports`) — agent layer owns the impl; **HE5b waits for it; no CriticDecide stub.**

Gates in scope (one engine + `HitlPolicy`/`GateConfig`, not per-gate code forks):
`spec_approval`, `plan` / `plan_approval`, `hypotheses`, `costly_or_nonreal`, `report`, `result_acceptance`, `skip_task`
(+ existing `budget` / `side_effect` / `proposed_check` stay on the same engine paths).

Plan IDs use **Q** / **HE** / **HN** / **CL** prefixes (not bare G1/G5/C1 — those clash with shared contracts).

---

## Semantics: `Result.unassessed` (CAP #7 / R23) — pick one path

**Correct (DESIGN §2 `Result.unassessed`, R23):** ids of **required ReviewCheck** rows still **without a verdict** (`UNASSESSED` or `PENDING`). They do **not** affect outcome (shown separately). `FAILED` is a verdict and must never appear here.

| Source | Behavior | Verdict |
|---|---|---|
| `checks/attachment.py` `unassessed_required_review_ids` | required + review only; UNASSESSED\|PENDING | **canonical** |
| `executor.py` (~1019 pending path) | **all** checks with PENDING\|UNASSESSED | **wrong** — includes CodeCheck + optional |
| `executor.py` (~1037 success path) | all `status == UNASSESSED` | **wrong** — same |

**Q3 immediately after Q0** replaces both executor list comps with the attachment helper so there is **no double path**.

---

## Open now (PR #52)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q0** | CheckAttachment helpers (R23) — *this PR (#52)* | — (on `main`) | Pure `checks/attachment.py`: require ≥1 CodeCheck + ≥1 ReviewCheck; `unassessed_required_review_ids` (required review only); refuse UNASSESSED↔FAILED. Tests per function. |

---

## After #52 merges (≤1 open PR; check `main` first)

### Quality #7

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q3** | Unify `Result.unassessed` → attachment helper | **Q0** | Delete executor inline list comps; both record paths call `unassessed_required_review_ids` only. No second semantics. |
| **Q1** | Wire `require_code_and_review` into `Executor.record` | **Q0**, prefer after **Q3** | Every `ResultRecorded`/`ResultPending` calls require; miss → typed refuse; both kinds kept in payload. |
| **Q2** | `ReviewScheduled` on every record exit | **Q1** | Exits that leave review UNASSESSED/PENDING emit `ReviewScheduled` (idempotent). |
| **Q4** | `review_deadline` → UNASSESSED | **Q2** | Past review deadline journals UNASSESSED (not FAILED); outcome via `compute_outcome`. |
| **Q5** | Catalog defaults require code+review pair | **Q0** | `load_catalog` refuses experiment defaults lacking both kinds. |
| **Q6** | `compute_outcome` matrix tests only | **Q0** | B2b MET+UNASSESSED→SUCCESS; only-review UNASSESSED→PARTIAL; FAILED stays FAILED. |
| **Q7** | CAPABILITIES #7 acceptance suite | **Q1–Q6**, **Q3** | `test_cap7_*` 1:1 with #7. |

### HITL #18 — engine (this agent)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **HE1** | GatePolicyOutcome pure map | **Q0** | Resolve timeout/headless/interactive outcome from `HitlPolicy.defaults` + `GateConfig` for **all** gates above; typed refuse if kind/policy missing. No guessed defaults. |
| **HE5a** | HITL engine: open / timeout / headless / human answer (**no delegate**) | **HE1** (may fold HE1 if ≤400 LOC); can start after **Q0** | One path in `experimental/hitl.py` for all gates driven by GateConfig + HE1. Headless never hangs (immediate policy). Timeout → policy map (`accept_computed` / `deny` / `fail_human_timeout` / …). Human accept/reject/skip P7. **No** critic/delegate/`AgentPort` in this PR. Harden in place — no second package. |
| **HE5b** | HITL engine: delegate via `AgentPort` | **HE5a**; **AgentPort impl from agent layer** (block — no stub) | `on_timeout=delegate|delegate_capped` and human «реши сам» call injected `AgentPort` only; `approved_by=critic`, `delegated_by=human|timeout`. |
| **HE6** | HITL deadline extend + tick order | **HE5a** | `HitlDeadlineExtended` ≤ task/study; tick closes due gates before finalize. |
| **HE7** | CAPABILITIES #18 acceptance suite | **HE5a–HE6** (delegate cases need **HE5b**) | `test_cap18_*` 1:1 with #18. |
| **HN1** | `docs/handoff/quality-hitl.md` | **HE5a** | Handoff: shipped, left (Q*, HE5b/AgentPort), extend a gate via config only. Then **STOP**. |
| **CL1** | CHANGELOG #7+#18 | **Q7**, **HE7** | Changelog only; no behavior change. |

### Cancelled / folded

- ~~Per-gate open/timeout PRs~~ → **HE5a**
- ~~CriticDecide Protocol + fake~~ → **HE5b** + real **`AgentPort`**
- ~~Second HITL path~~ → harden **`experimental/hitl.py` only**
- ~~Bare G1/G5/C1 ids~~ → **HE1 / HE5a / HE5b / CL1**

---

## Follow-ups (later PRs — not #52)

1. **Explicit check kind** — replace `not is_code ⇒ review` (`attachment.py` review_checks) with a typed kind/role field; no boolean complement.
2. **STALE / MIGRATION_ERROR** — decide whether they belong in unassessed, refuse, or separate surface (`attachment.py` coercion / status set).
3. **Wrong error type** — `CatalogError` on attachment/coercion miss (`attachment.py` require / refuse) → dedicated typed error (not catalog-load failure).
4. **Parametrize coercion tests** — `test_refuse_coercion_*` / same-status / other-pairs (`test_attachment.py` ~99–107).

---

## Next after this plan push

Land **Q0** (#52) → **Q3** (unify unassessed; kills executor double path) → **HE5a** (engine without delegate) → **HN1** handoff and STOP when that milestone is done. **HE5b** waits on agent-layer `AgentPort`.
