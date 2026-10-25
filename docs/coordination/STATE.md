# CoScientist-next coordination STATE

**Owner:** coordinator . Successors in chain: , . Prior: .

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:34 Europe/Moscow (UTC+3) |
|  tip |  (#171 MCP-J1) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #172–#179 (see below) |

## Merge policy

Standing grant: merge when adequacy gate passes (). . No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| **#171** | MCP-J1 @  →  |
| #168/#167 | RC17a / R16a.1a |
| #166/#169/#170 | G14a / STATE / event owners |
| #146→#161/#165 | launch CLI + STATE doc |

## Open — gate drive

| PR | Stream | Tip | Gate |
|----|--------|-----|------|
| **#173** | RC4 #100 |  | MERGEABLE+**CLEAN** — dual tip review in flight → **merge next** |
| **#172** | R16a.2+#121 |  | MERGEABLE+**CLEAN** — dual tip review in flight |
| **#176** | G8 #84 |  | MERGEABLE+**CLEAN** — dual re-review after restack |
| #178 | API #153 |  | MERGEABLE; CI settling; dual re-review |
| #177 | TG #88 |  | MERGEABLE; CI settling; dual re-review |
| #175 | RC8 #104 |  | MERGEABLE; **hold merge** until RC4→RC7r |
| #179 | STATE pulse |  | prefer over #174 |
| #174 | STATE 704a4fbf |  | CLEAN; supersede after #179 |

## In flight

| Stream | Agent |
|--------|-------|
| Dual #172 | bc-6bcdbb3f · bc-b5d33a35 (+ reconfirm) |
| Dual #173 | bc-4dd76b84 · bc-597aa938 (+ reconfirm) |
| Dual #176/#177/#178 | spawned |
| Dual #175 RC8 | bc-79a37c18 · bc-e6615518 |
| Restack G8/API/TG/RC8 | bc-77229692 (G8/API/TG done) |
| G18 | bc-0524f861 |
| RC5 | bc-10ec685d (replaced ERROR bc-421b6386) |

## Next after merges

1. #173 → continue RC5 #101
2. #172 → R16a.4 #122
3. #177 → G17
4. #171 done → G18 (in flight)
5. #176 → G7 consumers / RC9c when unblocked

## External blocker

Mac  — document only; do not invent.

## Status vocabulary

 |  |  |  | .
