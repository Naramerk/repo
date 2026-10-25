# Open PR queue (coordinator pulse)

Parent / successor: use `ManagePullRequest` + adequacy gate (`AGENTS.md`).
Do **not** merge until dual independent review is **review-ready (k=0)** on the **same tip SHA**.

Updated: 2026-10-25 16:28 Europe/Moscow.

| PR | Branch | Tip | Gate notes |
|----|--------|-----|------------|
| [#172](https://github.com/Naramerk/repo/pull/172) | `cursor/r16a3-nir-format-8e76` | `d67756e3daef16999f7b2df0d0460fef67abaa76` | dual review in flight |
| [#173](https://github.com/Naramerk/repo/pull/173) | `cursor/rc4-require-hypotheses-1681` | `9293f77857a78cce429ff38e3a02b08e95aa637a` | dual review in flight |
| [#174](https://github.com/Naramerk/repo/pull/174) | `cursor/coordination-state-pulse-704a4fbf` | (this docs tip) | docs/STATE — merge when CI green |

**Merged this pulse:** [#171](https://github.com/Naramerk/repo/pull/171) MCP-J1 tip `edcc735` — dual review-ready (k=0); now on `main` as `f3a0030`.

If this table is empty and product tips exist without PRs: open via `ManagePullRequest` immediately (do not park).
