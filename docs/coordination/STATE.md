# CoScientist-next coordination STATE

**Owner:** coordinator `bc-5b7a4e5e-a501-57c8-905c-155826545280`
(https://cursor.com/agents/bc-5b7a4e5e-a501-57c8-905c-155826545280).
Prior: `bc-b855755c…`, `bc-704a4fbf…`, `bc-530d81f6…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:05 Europe/Moscow (UTC+3) |
| `main` tip | `882756bd24e1c8f7687e7074a99467e63d29bb7e` (#183 RC5; includes #185/#186/#177/#178/#176) |
| Coordinator | https://cursor.com/agents/bc-5b7a4e5e-a501-57c8-905c-155826545280 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #188 RC6 · #182 G18 · #175 RC8 **HOLD** |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
This agent is **Task subagent** — cannot subscribe CI / Task-spawn; **parent must** subscribe CI on open heads and spawn successor.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync/status` | 0 — 316 files, 8666 nodes |
| CodeGraph query | `hypotheses artifact validate` → Artifact / validate_* |
| LeanCTX | `need.py` signatures (`assert_profile_hypotheses_satisfied`, `resolve_plan_needs`) |

## Merged (recent)

| PR | Note |
|----|------|
| **#183** | RC5 hypotheses artifact validate → `882756b` |
| **#185** | R16a.4 test split → `33b6ad4` |
| **#186** | STATE pulse5 |
| **#177** | TG-Call ToolCalled → `67816d7` |
| **#178** | API data_root compose → `5575763` |
| **#176** | G8 lifecycle → `d354f74` |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #188 RC6 | `ca945e4` | MERGEABLE; CI in flight; **needs dual k=0 on live tip** (restacked onto post-RC5 main) |
| #182 G18 | `765b3d6` | MERGEABLE; CI in flight; dual k=0 on `d80c7d3` + product-blob identity through rebase chain → merge when CLEAN + tip dual confirm |
| #175 RC8 | `7f2f5c4` | **HOLD** / CONFLICTING until RC5→RC6→RC7r (RC5 done) |

## In flight

| Stream | Note |
|--------|------|
| Dual RC6 `ca945e4` | bc-926d367e / bc-19e67653 (confirm tip match — tip moved from `b69bb16`) |
| Dual G18 `765b3d6` | bc-6b3cb777 / bc-3548edd8 |
| RC7r | bc-ff968929 / bc-45bb8130 (after RC6) |
| G17 | branches `cursor/graph-g17-tool-called-fold-1681` + `…toolcalled-1681` — restack onto main+#183, open PR, dual |
| RC8 restack | bc-669f4256 — only after RC6→RC7r |
| Soft cap | ~6 writers; many reviewers parallel OK |

## Next

1. Merge #188 when dual k=0 on `ca945e4` + CLEAN
2. Merge #182 when dual tip-confirm + CLEAN (independent of RC stack)
3. After RC6: dual RC7r → merge → restack/merge #175
4. G17 after TG (merged): open PR from restacked tip → dual → merge
5. **Parent:** subscribe CI on `cursor/rc6-selected-hyp-task-1681`, `cursor/graph-g18-mcp-fold-1681`, `cursor/rc8-hypothesis-verdict-1681`, STATE pulse branch; Task-spawn successor coordinator

## External blocker

Mac `runs_final_20261023` — document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
