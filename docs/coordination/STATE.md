# CoScientist-next coordination STATE

**Owner:** coordinator `bc-9424e63c-b220-5bfb-b57c-5b2e1148accd` (active). Chain successors: `bc-704a4fbf…` RUNNING, `bc-5e2c1d74…` pulse. Takeover from `bc-33626709-cebe-4d4f-9734-38a2f6c21681`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:31 Europe/Moscow (UTC+3) |
| `main` tip | `f3a0030c902c8a8c0fab06c9857d725c0a41f3c0` (#171 MCP-J1 merged) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #172 R16a.2+3 @ `5291fa1`; #173 RC4 @ `56acd7a`; #174 STATE pulse; #175 RC8; #176 G8; #177 TG-Call; #178 API |
| Successors | https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76 · https://cursor.com/agents/bc-5e2c1d74-ae3a-5fe6-ae78-3bec85acaeb6 |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). No per-PR ask. `gh pr merge --merge`.

## Merged (recent)

| PR | Note |
|----|------|
| **#171** | MCP-J1 Mcp*Recorded + pyproject sources @ `edcc735` → `f3a0030` |
| #168 / #167 | RC17a / R16a.1a |
| #166/#169/#170 | G14a / STATE / event owners |
| #146→#161 | launch CLI |
| #165 | STATE doc |

## Open — gate drive

| PR | Stream | Tip | Note |
|----|--------|-----|------|
| #172 | R16a.2+#121 R16a.3 | `5291fa1` | MERGEABLE; dual tip review in flight; CI subscribe |
| #173 | RC4 #100 | `56acd7a` | MERGEABLE; rebased on main; dual re-review in flight |
| #174 | STATE pulse 704a4fbf | (see PR) | docs; merge when CLEAN + tip review if needed |
| #176 | G8 #84 | was `9a2ebd0` | CONFLICTING → restack agent bc-77229692 |
| #178 | API #153 | was `7e16e53` | CONFLICTING → restack |
| #177 | TG-Call #88 | was `cf51b22` | CONFLICTING → restack |
| #175 | RC8 #104 | was `6e84fe9` | CONFLICTING; **hold merge** until RC4→RC7r |

## In flight (agents)

| Role | bcId |
|------|------|
| Dual review #172 | bc-6bcdbb3f · bc-b5d33a35 |
| Dual review #173 | bc-4dd76b84 · bc-597aa938 |
| Restack G8/API/TG/RC8 | bc-77229692 |
| RC5 implementer | bc-421b6386 |
| Prior dual reviews (tips may be stale after rebase) | see cloud agent list |

## Next after merges

1. After #172 → R16a.4 #122 test split (or close if included).
2. After #173 → RC5 #101 (already spawned bc-421b6386).
3. After #177 TG-Call → G17.
4. After #171 (done) → G18 MCP graph unlock.
5. After #176 G8 → continue G7 consumers / RC9c stack when unblocked.

## External blocker

Mac `runs_final_20261023` — not in VM; do not invent; document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
