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
| Checkpoint branch | `cursor/coordination-checkpoint-1681` @ `4535003` → PR #155 |
| Context restored from | live GitHub PR/CI + cloud agent metadata/transcripts; **not** from missing Mac handoff bundle |
| Prior-author transcripts | Recovered: all four claim **done** / awaiting next review; #138 author did **not** report main conflicts (GitHub: CONFLICTING — stream 2 correct) |

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

| PR | Branch | Tip SHA | Base | CI | Mergeable | Author | Status | Next |
|----|--------|---------|------|----|-----------|--------|--------|------|
| #146 | `cursor/m1-agents-adapter-822f` | `407a525` | main@7250dab | 10/10 | MERGEABLE | bc-a1a2f95d… IDLE | **review-ready** ([Review #146+#147](bc-8070d1ed-04e3-5bee-9745-5dee4467cf7d)) | Merge only with user permission |
| #147 | `cursor/m1-cli-run-822f` | `eb6502e` | #146 tip | 10/10 | MERGEABLE | same IDLE | **review-ready** (same) | Stack live-run follow-ups; merge with #146 |
| #150 | `cursor/m3-c1-coder-sandbox-109b` | `4ae4589` (`ecf6abc`) | behind main | 10/10 | MERGEABLE | bc-bfc104c9… IDLE | review pending WS3 | Independent review in flight |
| #138 | `cursor/tool-gap-payload-schemas-10ea` | `7865016` | main | tip green | **CONFLICTING** | stream 2 takeover | rebase in flight | Wait WS2 |
| #65 | `cursor/tool-gap-g3-catalog-resolved-10ea` | `0996804` | #138 tip | 10/10 | MERGEABLE | stream 2 | restack after #138 | Wait WS2 |
| #154 | `cursor/rc3t-study-thresholds-f754` | `dee10d5` | main@7250dab | 10/10 | MERGEABLE | bc-2bf00ea8… IDLE | review pending WS3 | Single author; review only |

### #146/#147 live-run residuals (follow-ups, not reopen of review-ready)

1. CLI still **cassette `StaticToolRouter` only** — need optional/production MCP ToolRouter path without inventing tools.
2. **LIT/HYP unwired; REPORT cancelled** — honest today; need real roles when catalog/profile allows.
3. LLM client wired; real endpoint depends on `--llm-config`; tools remain cassette until (1).

## Active workstreams (max 3)

| # | Stream | Role | Owner agent | Branch/PR | Goal | Status |
|---|--------|------|-------------|-----------|------|--------|
| 1 | Launch path review | reviewer | [bc-8070d1ed](https://cursor.com/agents/bc-8070d1ed-04e3-5bee-9745-5dee4467cf7d) | #146+#147 | Verdict tips | **done** → review-ready |
| 1b | Live-run composition | implementer | *(assigning)* | new stack on #147 | MCP ToolRouter path + honest role wiring | starting (slot freed) |
| 2 | Event schemas rebase | implementer | [bc-43883508](https://cursor.com/agents/bc-43883508-110a-5472-bb36-559df8c1b0e5) | #138→#65 | Rebase + restack | RUNNING |
| 3 | Sandbox + thresholds review | reviewer | [bc-5b6d027c](https://cursor.com/agents/bc-5b6d027c-4eaf-593a-98a3-60886909770f) | #150+#154 | Tip verdicts | RUNNING |

Do **not** merge #146/#147 without explicit user permission. Do not reopen #146/#147 author branches for live-run — stack a **new** PR on `eb6502e`.

## Dependency order (product)

1. ~~Integrate/review #146 → #147~~ → **review-ready**; await merge permission; parallel live-run stack.
2. Rebase #138 → restack #65; wire resolve through effect/`is_fetch` + `invoke_tool`/provenance.
3. Accept #150 coder sandbox; connect to workflow + Linux preflight.
4. Production profile/catalog + real roles; durable fork.
5. Regressions when fixtures available.

## Status vocabulary

Use only: `implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`
plus worker `done`/`partial`/`blocked`.
