# CoScientist-next — agent operating rules

This file is the **coordinator-owned** operating contract for Cursor agents working in
`Naramerk/repo` (CoScientist-next). It does **not** replace design docs under `docs/`
(`DESIGN.md`, `CAPABILITIES.md`, plan slices). Do not weaken scientific / fail-closed
gates for green tests.

## Authority

- **Coordinator** owns `docs/coordination/STATE.md`, merge decisions (see Merge
  authority), and workstream assignment.
- **One author per branch.** Before taking over a branch, confirm the previous agent is
  IDLE/killed and record the transfer in STATE + handoff. Never assign two authors to
  the same PR/branch.
- Preserve existing commits and open PRs; do not recreate merged work (#149/#151/#152)
  or spawn a second parallel backend/runner for the same task.
- Distinguish: `implemented` / `mock-verified` / `live-verified` / `review-ready` /
  `merged`. Green CI ≠ research readiness.

## Code quality

Prefer reuse and deletion over layered add-ons. Clean, clear code beats fewer review
rounds or a faster merge. Do not paper over contract drift with a second path.

## Changelog

Every PR adds exactly one fragment `changelog.d/<topic>.<type>.md`
(`<type>`: `cleanup`/`feat`/`fix`/`refactor`; plain markdown, no leading `-`)
and does not edit `CHANGELOG.md` — any diff on it fails the gate. The gate
(`scripts/check_changelog_fragment.py`, CI job `changelog-fragment`)
enforces this; `towncrier build` renders fragments into `CHANGELOG.md` at
release time as a manual maintainer operation outside the gate (direct
commit, or a PR with an expectedly red gate merged by owner decision). See
`changelog.d/README.md`.

## Mandatory context tools (every agent, every session)

Install and use **both**:

| Tool | Pin (start) | Upstream |
|------|-------------|----------|
| CodeGraph | `@colbymchenry/codegraph@1.6.2` | https://github.com/colbymchenry/codegraph |
| LeanCTX | `lean-ctx-bin@3.11.4` | https://github.com/yvgude/lean-ctx |

Setup (idempotent):

```bash
bash scripts/setup_context_tools.sh
export PATH="$HOME/.local/share/coscientist-context-tools/node_modules/.bin:$PATH"
```

**Preflight before product work** (record exit codes + logs in handoff / STATE):

1. `codegraph --version` → `1.6.2`
2. `lean-ctx --version` → `3.11.4`
3. `codegraph status "$PWD"` (run `codegraph init -y "$PWD"` if missing; after branch/code
   change: `codegraph sync "$PWD"`)
4. One substantive CodeGraph query (`query` / `node` / `callers` / `callees` / `impact`)
5. One LeanCTX source read (`lean-ctx read FILE -m signatures` or `lines:N-M`)

A config file without a working CLI call is **not** installed. MCP entries inside a VM
do not prove the current agent process has MCP; CLI is the required fallback.

Before changing an important contract, use CodeGraph impact/callers and LeanCTX
anchored reads on call sites. Empty static search ≠ proof of no dynamic call — check
sources and behavior.

## Roles

Project profiles (model: inherit):

- `.cursor/agents/coscientist-implementer.md`
- `.cursor/agents/coscientist-reviewer.md`
- `.cursor/agents/coscientist-verifier.md`

Prefer more parallel agents for **independent** tasks (especially read-only
reviews). Soft cap ~6 writing workstreams; no hard cap on parallel reviewers.
Each **write** stream still owns one branch/checkout (one author per branch).
Agree shared contracts before parallel edits on the same files.

## Assignment packet (required)

Every subagent assignment must include: repo + base SHA + PR/branch + goal + file/contract
bounds; coordinator checkpoint branch/SHA; commits/review notes to preserve or close;
order to read `AGENTS.md`, `docs/coordination/STATE.md`, and the role profile; both
context tools + own index; acceptance criteria + check commands; dependencies/current
author; return format.

## Return packet (required)

`status` (`done`/`partial`/`blocked`); commit SHA/branch/PR; what changed and why;
checks with exit codes/logs; CodeGraph queries + LeanCTX calls used; open review items;
remaining risks/deps; next concrete step.

## Legacy comparison

New code = this repo. Legacy CoScientist and `runs_final_20261023` live on the user's Mac
and are **not** auto-mounted here. Missing fixtures are concrete blockers — do not invent
inputs. Compare substantive results/artifacts/usage/cost/time, not only `fulfilled`.

## Cloud environment

Keep Python + sandbox deps (bubblewrap, cgroup delegation) as in CI
(`.github/workflows/ci.yml`). Layer context-tools setup via
`scripts/setup_context_tools.sh` in install/start when the environment build can run it.
Personal environment.json may be owner-restricted; if an admin UI action is required,
agents must request that single concrete action instead of claiming MCP is wired.

## Merge authority

Standing grant (permanent unless revoked): merge without a per-PR ask when the
adequacy gate passes — do not wait for another «разрешаю».

1. Independent review **review-ready** (k=0) on the tip
2. `mergeable=MERGEABLE` and `mergeStateStatus=CLEAN`
3. Required CI green on that tip
4. Tip SHA matches the reviewed tip (or pure rebase)
5. Stack order preserved

Gate fails → do not merge. Gate passes → merge immediately.
