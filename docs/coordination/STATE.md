# CoScientist-next coordination STATE

**Owner:** coordinator `bc-b855755c-72d8-5a5f-9204-b683ccec50e7`
(https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7).
Prior: `bc-704a4fbf…`, `bc-530d81f6…`, `bc-9424e63c…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:05 Europe/Moscow (UTC+3) |
| `main` tip | `882756bd24e1c8f7687e7074a99467e63d29bb7e` (#183 RC5) |
| Coordinator | https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #188 RC6 · #182 G18 · #175 RC8 **HOLD** (DIRTY — restack after RC6/RC7r) |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Task subagent limits: no CI subscribe / no Task-spawn / no `gh pr create` — parent must cover.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` |
| `lean-ctx --version` | `3.11.4` |
| `codegraph init/sync` | 0 |
| CodeGraph query | `max_attempts` |
| LeanCTX | `lifecycle.py` signatures |

## Merged (recent)

| PR | Note |
|----|------|
| **#183** | RC5 @ `9694bdf` → `882756b` (dual k=0 on `4073bf1` + pure rebase) |
| **#185** | R16a.4 → `33b6ad4` |
| **#186** | STATE pulse5 |
| **#177** | TG-Call |
| **#178** | API data_root |
| **#176** | G8 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #182 G18 | `765b3d6` | dual k=0 on `d80c7d3` + product blobs identical; CI settling |
| #188 RC6 | `ca945e4` | on main+#183; needs dual review |
| #175 RC8 | `7f2f5c4` | **HOLD** + CONFLICTING until RC6→RC7r then restack |

## Next

1. Merge #182 when CI CLEAN on `765b3d6`
2. Dual-review #188 → merge → RC7r → restack/merge #175
3. Parent: open STATE pulse6 PR (`cursor/coordination-state-pulse6-50e7`); subscribe CI; spawn successor

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
