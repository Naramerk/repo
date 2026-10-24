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

**Correct (DESIGN §2 `Result.unassessed`, R23):** ids of **required ReviewCheck** rows still **without a verdict** (`UNASSESSED` or `PENDING`). They do **not** affect outcome (shown separately). `FAILED` is a verdict and must never appear here. FAILED/STALE/MIGRATION_ERROR stay distinct (DESIGN §5.4).

| Source | Behavior | Verdict |
|---|---|---|
| `checks/attachment.py` `unassessed_required_review_ids` | required + review only; UNASSESSED\|PENDING | **canonical** |
| `executor.py` (~1019 pending path) | **all** checks with PENDING\|UNASSESSED | **wrong** — includes CodeCheck + optional |
| `executor.py` (~1037 success path) | all `status == UNASSESSED` | **wrong** — same |

**Q3b** replaces both executor list comps with the attachment helper so there is **no double path**.

---

## Landed / open

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q0** | CheckAttachment helpers (R23) | — | #52 landed. |
| **Q0d** | Attachment allowlist + agent `check_detail` — *this PR (#68)* | **Q0** | `CheckLike` / agent view / `CheckResult` share `is_code`; UNASSESSED allowlist from §5.4 partners; agent view `is_code` + `check_detail` (match/assert_never, no mixed getattr); `refuse_illegal_unassessed_transition`; agent-facing no `or {}`/`or []`/str-bool; `template_to_requirement(req_id=…)` required. |

Debt not claimed closed here:
- **Q3a:** introduce `CheckKind`; `CheckResult.check_kind` replaces `is_code` (one owner); migrate `CheckLike` + agent view + `reviews.py` `getattr(c,'is_code',True)` fail-open; `_evaluate_proposed_check` `is_code=row.get("check") is not None` (five sites).
- **Q8:** fail-closed remaining `evaluate.py` silent `or`/getattr-default/bool-coerce (no new fallbacks on Q0d-touched lines). By symbol:
  - `_effect_provenance_fields` / `artifact_belongs_to_attempt`: `getattr(meta,"producer","agent")` else `"agent"`; `produced_by`/`call_id` `str(... or "")`; `str(artifact_id or "")`; `bool(belongs)`; `getattr(...,"net_isolation"|"fs_isolation",None)`.
  - `evaluate_code_check`: `check.output or ""|"main"`; `check.op or "=="`.
  - `_evaluate_proposed_check`: `proposal_id|status or`; `bound_* or`; `claims or []`; `target or ""`; `reject_reason or`; `bool(delegated|uses_sesoi|predicts_null)`; `predicted_direction or "two_sided"`; `effect or {}`.
  - `evaluate_task_checks`: `* or {}`; `str(task_kind or "")`; `str(ref.template_id)`; plan_expansion `str(... or "")`; `expected_outputs or []`; `check.output or "main"`; `bool(tmpl.uses_sesoi)`; `bool(tmpl.predicts_null)`; `str(tmpl.predicted_direction)`.

---

## After Q0d (≤1 open PR; check `main` first)

### Quality #7

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **Q3a** | `CheckResult.check_kind` source of truth | **Q0d** | Fill from catalog/proposal; migrate reviews + evaluate inferences. |
| **Q3b** | Unify `Result.unassessed` → attachment helper | **Q3a** | Delete executor inline list comps; both record paths call `unassessed_required_review_ids` only. |
| **Q8** | Fail-closed evaluate.py or/coercions | **Q0d** | Named PR for silent `or`/coercions listed under debt (not Q0d/Q3a). |
| **Q1** | Wire `require_code_and_review` into `Executor.record` | **Q3b** | Every `ResultRecorded`/`ResultPending` calls require; miss → typed refuse; both kinds kept in payload. |
| **Q2** | `ReviewScheduled` on every record exit | **Q1** | Exits that leave review UNASSESSED/PENDING emit `ReviewScheduled` (idempotent). |
| **Q4** | `review_deadline` → UNASSESSED | **Q2** | Past review deadline journals UNASSESSED (not FAILED); outcome via `compute_outcome`. |
| **Q5** | Catalog defaults require code+review pair | **Q0d** | `load_catalog` refuses experiment defaults lacking both kinds. |
| **Q6** | `compute_outcome` matrix tests only | **Q0** | B2b MET+UNASSESSED→SUCCESS; only-review UNASSESSED→PARTIAL; FAILED stays FAILED. |
| **Q7** | CAPABILITIES #7 acceptance suite | **Q1–Q6**, **Q3b** | `test_cap7_*` 1:1 with #7. |

### HITL #18 — engine (this agent)

| ID | Title | Depends | Acceptance |
|---|---|---|---|
| **HE1** | GatePolicyOutcome pure map | **Q0d** | Resolve timeout/headless/interactive outcome from `HitlPolicy.defaults` + `GateConfig` for **all** gates above; typed refuse if kind/policy missing. No guessed defaults. |
| **HE5a** | HITL engine: open / timeout / headless / human answer (**no delegate**) | **HE1** (may fold HE1 if ≤400 LOC); can start after **Q0d** | One path in `experimental/hitl.py` for all gates driven by GateConfig + HE1. Headless never hangs (immediate policy). Timeout → policy map (`accept_computed` / `deny` / `fail_human_timeout` / …). Human accept/reject/skip P7. **No** critic/delegate/`AgentPort` in this PR. Harden in place — no second package. |
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
- ~~Follow-ups (kind / STALE / CatalogError / parametrize)~~ → closed in **Q0d** (remaining → **Q3a** / **Q8**)

---

## Next

**Q3a** → **Q3b** → … **HE5a** → **HN1** STOP. **Q8** may parallel after Q0d. **HE5b** waits on agent-layer `AgentPort`.
