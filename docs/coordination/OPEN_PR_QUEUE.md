# Open PR queue (coordinator pulse)

Parent / successor: use `ManagePullRequest` + adequacy gate (`AGENTS.md`).
Do **not** merge until dual independent review is **review-ready (k=0)** on the **same tip SHA**.

Updated: 2026-10-25 16:25 Europe/Moscow.

| PR | Branch | Tip | Gate notes |
|----|--------|-----|------------|
| [#171](https://github.com/Naramerk/repo/pull/171) | `cursor/mcp-j1-recorded-1681` | `edcc7350e2518d15a522eb9b5399de36d19dc3dd` | MERGEABLE+CLEAN; waiting dual re-review (prior `ab83a8d` k=1 closed by this tip) |
| [#172](https://github.com/Naramerk/repo/pull/172) | `cursor/r16a3-nir-format-8e76` | `d67756e3daef16999f7b2df0d0460fef67abaa76` | MERGEABLE; CI + dual review in flight |
| [#173](https://github.com/Naramerk/repo/pull/173) | `cursor/rc4-require-hypotheses-1681` | `9293f77857a78cce429ff38e3a02b08e95aa637a` | MERGEABLE; PR checks + dual review in flight |

If this table is empty and product tips exist without PRs: open via `ManagePullRequest` immediately (do not park).
