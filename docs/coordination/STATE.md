# CoScientist-next coordination STATE

**Owner:** coordinator `bc-5b7a4e5e-a501-57c8-905c-155826545280`
(https://cursor.com/agents/bc-5b7a4e5e-a501-57c8-905c-155826545280).
Priors: `bc-2d2097c2` pulse7 · `bc-b855755c` · `bc-704a4fbf`. Concurrent: pulse8/9.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:12 Europe/Moscow (UTC+3) |
| `main` tip | `d01e8df` (#190 STATE pulse6-accd; includes #188 RC6 + #182 G18 + #183 RC5) |
| Coordinator | https://cursor.com/agents/bc-5b7a4e5e-a501-57c8-905c-155826545280 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #192 RC7r · #191 RC6 post-cap · #189 G17 · #175 RC8 **HOLD** |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
This agent is **Task subagent** — cannot subscribe CI / Task-spawn / `gh pr create`. **Parent must** subscribe CI + keep successor alive.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | 0 — 316 files |
| CodeGraph query | `hypotheses artifact validate` |
| LeanCTX | `need.py` signatures |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#190** | STATE pulse6-accd → `d01e8df` |
| **#188** | RC6 selected hyp ≥1 task → `eccaf56` (**note:** dual had k=1 post-cap; #191 hotfix) |
| **#182** | G18 mcp_exchange → `bac4c62` (dual k=0 + pure rebase) |
| **#183** | RC5 hypotheses artifact → `882756b` |
| #185/#177/#178/#176/#171 | R16a.4 / TG / API / G8 / MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#192 RC7r** | `2e76d3e` | MERGEABLE; CI in flight; **needs dual k=0** → then merge (unblocks #175) |
| **#191 RC6 fix** | `2b74214` | post-cap `assert_selected_hyp_ref` in `materialize_foreach`; local pytest 7 pass; dual + CLEAN → merge |
| **#189 G17** | `6744ec6` | restacked atop G18; ingest contract includes `graph_fold.mcp`; dual + CLEAN → merge |
| **#175 RC8** | `5cb9fb7` | CLEAN but **HOLD** until RC7r (#192) merges → restack → dual → merge |

## In flight

| Stream | Note |
|--------|------|
| Dual #191 / #192 / #189 | parent-spawned reviewers; tip-match required |
| Soft cap | ~6 writers **exceeded** — no new writers |
| Successors | pulse8 `bc-157abd96`, pulse9 `bc-036c2b4b` already running |

## Next

1. Merge #191 when dual k=0 + CLEAN
2. Merge #192 RC7r when dual k=0 + CLEAN
3. Merge #189 G17 when dual k=0 + CLEAN (independent of RC stack)
4. After #192: restack #175 → dual → merge
5. **Parent:** subscribe CI on #189/#191/#192/#175 heads; keep successor coordinator; do not park

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
