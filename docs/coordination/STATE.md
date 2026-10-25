# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 05:15 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint branch | `cursor/coordination-checkpoint-1681` @ `683cb7e` → PR #155 |
| Context restored from | live GitHub PR/CI + cloud agent metadata/transcripts; **not** from missing Mac handoff bundle |

## Context-tools preflight (coordinator VM)

| Check | Result |
|-------|--------|
| CodeGraph | `1.6.2` at `~/.local/share/coscientist-context-tools` |
| LeanCTX | `3.11.4` |
| `codegraph status /workspace` | 271 files, 7307 nodes, up to date |
| Sample query | `invoke_tool` → `coscientist/tools/effects.py:530` (20 callers indexed) |
| LeanCTX read | `coscientist/tools/gate.py` signatures OK (after `lean-ctx allow-path /workspace`) |

## Missing inputs (explicit)

Requested bootstrap files were **not** present in this Cloud VM or on `main`:

`README.txt`, `START-PROMPT.txt`, `AGENTS.coordination.fragment.md`,
`BOOTSTRAP-HANDOFF.json`, `legacy-baseline-manifest.json`, `architecture-evidence.md`,
`baseline-evidence.md`, `validation-evidence.md`, `validation-versions.txt`.

Legacy Mac paths (`/Users/nargiza/.../CoScientist`, `.../runs_final_20261023`) are not
mounted. Scenario fixtures must be supplied separately before live legacy replay.

## Merged (do not recreate)

| PR | Note |
|----|------|
| #149 | typed MinimalReport |
| #151 | cancel + FastAPI/WS |
| #152 | report formats → tip of `main` |

## Open critical path

| PR | Branch | Tip SHA | Base | CI | Mergeable | Prior author | Author status | Tip vs last review | Next |
|----|--------|---------|------|----|-----------|--------------|---------------|--------------------|------|
| #146 | `cursor/m1-agents-adapter-822f` | `407a525` | `main`@7250dab | 10/10 | MERGEABLE | bc-a1a2f95d… | IDLE | tip **after** review@6d8eb6d; claims CallCtx→`core.call_ctx` + budget accumulate | Independent review of tip |
| #147 | `cursor/m1-cli-run-822f` | `eb6502e` | #146 tip | 10/10 | MERGEABLE | same | IDLE | tip **after** review; claims required `--hitl-mode` | Review after/with #146 |
| #150 | `cursor/m3-c1-coder-sandbox-109b` | `4ae4589` (fix `ecf6abc`) | main behind tip by #149/#151/#152 | 10/10 | MERGEABLE | bc-bfc104c9… | IDLE | tip **after** review@b753cbb; claims cgroup.events + re-raise | Independent review; then rebase onto main |
| #138 | `cursor/tool-gap-payload-schemas-10ea` | `7865016` | main | 5/5 on tip | **CONFLICTING** | bc-ea9710c3… | IDLE | tip after review@cef2b97 | Rebase onto 7250dab, keep Cayman closes |
| #65 | `cursor/tool-gap-g3-catalog-resolved-10ea` | `0996804` | #138 tip | 10/10 | MERGEABLE | same | IDLE | tip after review | Restack after #138 rebase |
| #154 | `cursor/rc3t-study-thresholds-f754` | `dee10d5` | main@7250dab | 10/10 | MERGEABLE | bc-2bf00ea8… | IDLE | tip **after** review@1caed88; merge-tree clean vs main | Independent review of tip only (single author) |

## Active workstreams (max 3)

| # | Stream | Role | Owner agent | Branch/PR | Goal |
|---|--------|------|-------------|-----------|------|
| 1 | Launch path review | reviewer | [bc-8070d1ed](https://cursor.com/agents/bc-8070d1ed-04e3-5bee-9745-5dee4467cf7d) | #146+#147 tips | Verdict on 407a525 / eb6502e; residual live-run gaps |
| 2 | Event schemas rebase | implementer | [bc-43883508](https://cursor.com/agents/bc-43883508-110a-5472-bb36-559df8c1b0e5) | #138→#65 | Rebase #138 on 7250dab; restack #65; takeover from IDLE bc-ea9710c3 |
| 3 | Sandbox + thresholds review | reviewer | [bc-5b6d027c](https://cursor.com/agents/bc-5b6d027c-4eaf-593a-98a3-60886909770f) | #150+#154 tips | Verdict on ecf6abc/4ae4589 and dee10d5 |

Prior sand authors remain sole product authors on #146/#147/#150/#154 (reviewers do not
push). Stream 2 records IDLE takeover of #138/#65 from bc-ea9710c3.

## Dependency order (product)

1. Integrate/review #146 → #147 (honest CLI study cycle, budgets, HITL).
2. Rebase #138 → restack #65; wire resolve through authoritative effect/`is_fetch` +
   `invoke_tool`/provenance (exact catalog; no blanket unknown-tool allow).
3. Accept #150 coder sandbox; connect to workflow + Linux preflight (no isolation bypass).
4. Production profile/catalog + real roles; then durable fork (child journal, lineage,
   inherited artifacts, affected-only redo, idempotency/restart).
5. Regressions when fixtures available: article_small → two_tasks → hypothesis_check →
   file_handoff → full legacy set.

## Status vocabulary

Use only: `implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`
plus worker `done`/`partial`/`blocked`.
