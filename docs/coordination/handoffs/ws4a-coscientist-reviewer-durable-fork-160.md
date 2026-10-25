# WS4a coscientist-reviewer — PR #160 durable fork child journal

**Reviewer:** https://cursor.com/agents/bc-f5db1af8-1766-51e7-8283-a640f816c650  
**Coordinator:** https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681  
**Author:** https://cursor.com/agents/bc-9780a7e0-957c-5922-84c1-a803bfcf6a1a  
**Checkpoint:** `cursor/coordination-checkpoint-1681`  
**Observation:** 2026-10-25 (UTC)  
**status:** `done`  
**No product pushes; author branch untouched.**

## Context-tools preflight

| Check | Result | exit |
|-------|--------|------|
| `bash scripts/setup_context_tools.sh` (from checkpoint) | codegraph 1.6.2 + lean-ctx 3.11.4 | 0 |
| `codegraph --version` | `1.6.2` | 0 |
| `lean-ctx --version` | `3.11.4` | 0 |
| `codegraph init -y` / `status` tip | 272 files, 7346 nodes, up to date | 0 |
| CodeGraph `query materialize_fork_journal` | `fork_journal.py:227` + hitl import | 0 |
| CodeGraph `callers materialize_fork_journal` | only `HitlForkMixin.fork` (`hitl.py:676`) | 0 |
| CodeGraph `impact materialize_fork_journal` | hitl + 7 continue_session tests | 0 |
| LeanCTX `read fork_journal.py -m signatures` | `materialize_fork_journal`, `link_inherited_artifact`, … | 0 |
| LeanCTX `read hitl.py lines:676-704` | fork → `materialize_fork_journal` | 0 |
| LeanCTX `read fork_journal.py lines:121-156` | CAS copy fail-closed | 0 |
| LeanCTX `read fork_journal.py lines:334-356` | write-once `ForkCreated` | 0 |

## Target

| Field | Value |
|-------|-------|
| PR | [#160](https://github.com/Naramerk/repo/pull/160) |
| Branch | `cursor/durable-fork-child-journal-1681` |
| Tip | `83bafba81afa0229c5fa7ad882ec392fc7d74092` |
| Base | `main` @ `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| merge-base | `7250dab` (== main tip) |
| Diff | +651/−6 across 5 files (`fork_journal.py` new) |
| **Verdict** | **`review-ready`** (k=0) |

## Review bar

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Child journal distinct; lineage `parent_study_id` / `ForkCreated` | **Pass** — separate `data_root/<child_id>/events.db`; `StudyCreated` + graph attrs + `ForkCreated` payload; parent events stay `st_fork` |
| 2 | Inherited artifacts CAS-copied; no invented bytes | **Pass** — `link_inherited_artifact` reads parent CAS, `put_cas`, digest match refuse; missing meta/CAS → `ContinueSessionRefuse` |
| 3 | Only affected re-opened; idempotent re-fork; write-once `ForkCreated` | **Pass** — affected → `TaskCreated` PENDING; DONE inherit → `TaskInherited`; re-fork same idem skips existing; write-once avoids digest `Conflict` when parent task set grows (reviewer repro: `EXP-X` appended after first fork → re-fork OK, 1× `ForkCreated`, new key inherited) |
| 4 | `hitl.fork` materializes (not partition-only) | **Pass** — calls `materialize_fork_journal`; still returns `Self` with `last_fork` (API shape unchanged; child via `last_fork.child_id`) |
| 5 | Fail-closed typed refuses; CI green | **Pass** — `ContinueSessionRefuse` codes throughout; PR CI `40157114536` success on tip; push CI `40156907037` success |

## Checks (local + CI)

| Check | exit | notes |
|-------|------|-------|
| `pytest tests/test_continue_session.py` | 0 | 47 passed |
| `ruff check` (touched) | 0 | |
| Grown-parent re-fork repro (ad-hoc) | 0 | no `Conflict`; child gains `EXP-X` |
| PR CI `40157114536` @ tip | 0 | ci + ingest-tar×3 + perf SUCCESS |
| Push CI `40156907037` @ tip | 0 | prior green |

## Blockers

**None (k=0).**

## Follow-ups (non-blocking)

- Commit message for `83bafba` cites “property test” for grown-parent write-once; tip has restart-safe duplicate coverage but **no dedicated grown-parent property test** (behavior verified ad-hoc by reviewer).
- DESIGN §3.6 p.4 PLAN auto-replan / §3.6 p.7 GC / full CLI `continue_session` still need launch stack (honest in PR body).
- `fork()` returns parent `Self` (not child runtime / study_id); callers must use `last_fork.child_id` — consistent with prior experimental API.

## Next step

Coordinator: mark #160 **review-ready** in STATE; merge only with explicit user permission. No author action required for tip `83bafba`.
