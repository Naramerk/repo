# WS3 coscientist-reviewer — PR #150 + #154

**Reviewer:** https://cursor.com/agents/bc-5b6d027c-4eaf-593a-98a3-60886909770f  
**Checkpoint:** `cursor/coordination-checkpoint-1681` @ `683cb7e`  
**Observation:** 2026-10-25 (UTC)  
**status:** `done`  
**No product pushes; author branches untouched.**

## Context-tools preflight

| Check | Result | exit |
|-------|--------|------|
| `codegraph --version` | `1.6.2` | 0 |
| `lean-ctx --version` | `3.11.4` | 0 |
| `codegraph status /workspace` | 271 files, 7307 nodes, up to date | 0 |
| CodeGraph query | `invoke_tool` → `coscientist/tools/effects.py:530` | 0 |
| LeanCTX read | `coscientist/tools/gate.py` signatures OK | 0 |
| Index `/tmp/review/pr150` @ `4ae4589` | 264 files / 7206 nodes | 0 |
| Index `/tmp/review/pr154` @ `dee10d5` | 273 files / 7369 nodes | 0 |
| Tip query #150 | `CgroupEventsLiveness`, `GuestProcessGroupGone` + impact/callers | 0 |
| Tip LeanCTX #150 | `run.py` lines 75–107 (`cgroup.events`) | 0 |
| Tip query #154 | `require_threshold_int` / `require_threshold_bool` | 0 |
| Tip LeanCTX #154 | `thresholds.py` signatures | 0 |

## A) PR #150 — tip `4ae4589` (fix `ecf6abc`)

| Field | Value |
|-------|-------|
| Branch | `cursor/m3-c1-coder-sandbox-109b` |
| Tip | `4ae4589f458edbe0afaf42cd7aa2116c37ebbd2b` |
| Substantive fix | `ecf6abc6cdba1bed3a26c0aeb1cdd108d968ffa8` |
| Prior author | `bc-bfc104c9-…` IDLE (sole product author; not taken over) |
| Base vs `main` | merge-base `bba8e43`; **behind** `main`@`7250dab` by #149/#151/#152 |
| merge-tree vs `main` | clean (`git merge-tree --write-tree` exit 0) |
| CI | 10/10 SUCCESS (`gh pr checks` exit 0) |
| **Verdict** | **`review-ready`** (content). **Rebase onto `main` before merge.** |

### Prior gorilla claims @ `b753cbb` → tip

| Claim | Verified |
|-------|----------|
| Liveness via `cgroup.events` / `CgroupEventsLiveness` (not `/proc`) | Yes — `run.py:75-107`; path `leaf/cgroup.events` `populated`; missing/bad → `ToolError` |
| `on_guest_cgroup` publish | Yes — sandbox hook + `_set_guest_cgroup`; unpublished → `record_cancel_failed` |
| TOCTOU `GuestProcessGroupGone`: journal then **raise** | Yes — `_stop_guest_or_record` L686-693 and `finally` L854-861 both `record_*` then bare `raise` |
| No except→return / audit-ok / swallow | Yes — AST: all 7 `except` in `run.py` end with `raise`; no `audit-ok` markers; `/proc` only in docstring negation |
| Tests | Real `cgroup.events` fixtures + `_FakeLiveness`; match= re-raise sites |

### Isolation honesty (spot-check)

- Coder passes `result.net_isolation` / `fs_isolation` into `put_sandbox` (no invent).
- `run_sandbox` remains fail-closed without hard FS isolation; no bypass recommended or present in tip.
- Local: `pytest tests/tools/test_coder_job.py -k 'cgroup_events or toctou or guest_process_group…'` → **17 passed, 1 skipped**, exit 0.

### Blockers remaining

**None on tip content.** Merge prerequisite: rebase onto `7250dab` (currently clean merge-tree; still required so tip is based on current `main`).

---

## B) PR #154 — tip `dee10d5`

| Field | Value |
|-------|-------|
| Branch | `cursor/rc3t-study-thresholds-f754` |
| Tip | `dee10d5c1e63be34be3e32ab6c589117df2e481c` |
| Sole author (assignment) | `bc-2bf00ea8-…` IDLE — **no second implementer assigned** |
| Base | `main`@`7250dab` (merge-base == main tip) |
| merge-tree vs `main` | clean (exit 0); `mergeable=MERGEABLE`, `mergeStateStatus=CLEAN` |
| CI | 10/10 SUCCESS |
| Last review | @`1caed88` — **4 blockers** |
| **Verdict** | **`review-ready`** (k=0) |

### Prior blockers @ `1caed88` vs tip

| # | Prior blocker | Tip status |
|---|---------------|------------|
| 1 | merge-tree conflicts vs `7250dab` | **Closed** — clean |
| 2 | ~135 pure-reflow hunks + out-of-scope `_timeout_kind`/`_refute_unresolved` | **Closed** — those helpers **byte-equal to main**; residual pure hunks = **8 blank-line moves** only (non-blocking) |
| 3 | `terminal_reason_for` compressed in `test_finalize_totality.py` | **Closed** — multi-line form matches main |
| 4 | `k: object` / threshold kwargs `object` | **Closed** — `k: int \| None`; verdict kwargs `int`/`bool`; `require_*` still refuses bool-as-int; `k=True` → `CatalogError` match=`confirmations_needed_must_be_int_got_bool` |

### Checks (local)

| Command | exit |
|---------|------|
| `pytest tests/test_rc3t_study_thresholds.py` | 0 (51 passed) |
| `pytest tests/test_finalize_totality.py -k terminal_reason` | 0 (2 passed) |
| `python scripts/audit_heuristics.py --vs-main` | 0 (0 hits, 0 audit-ok) |
| `ruff check` thresholds/dispositions/verdict/upcasters | 0 |

### Blockers remaining

**None.**

---

## Review-ready summary

| PR | Tip | review-ready? | Next for coordinator |
|----|-----|---------------|----------------------|
| #150 | `4ae4589` | **yes** (rebase before merge) | Assign implementer takeover of IDLE `bc-bfc104c9` **or** user-approved rebase onto `7250dab`; re-CI; then merge when permitted |
| #154 | `dee10d5` | **yes** | Eligible to merge when user permits; keep sole author `bc-2bf00ea8` (do not spawn second implementer) |

**No merge performed.**
