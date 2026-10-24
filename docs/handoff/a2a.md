# Handoff: capability #21 — A2A / MCP external MAS

## Область

Capability **#21** (docs/CAPABILITIES.md) / DESIGN **§6.5**:

1. **A2A inbound** — facade creates a study and streams journal events (agent card, task → study).
2. **A2A outbound** — remote agent callable as a `Tool` (`provider=http`, injected transport).
3. **MCP server facade** — publish CoScientist modules (hypotheses / literature / plan) as MCP tools via injected handlers.

Constraints for follow-up work:

- ≤ **400** lines per PR (diff vs base); ≤ **1** open PR; stack depth ≤ **2**; **no shims**.
- Optional feature: config or typed refusal (`IntegrationDisabled`) — no hardcodes/heuristics.
- Depend on **narrow Protocols**; test with **fakes** (agent layer / MCP client land in parallel).
- import-linter: `integrations` ↛ `core` / `api` / `tools` / `mcp` / `httpx`; `api`/`tools` ↛ `mcp`; httpx only in `coscientist.mcp.httpx_transport`.
- `scripts/audit_heuristics.py --vs-main` must pass; markers hand-only.

## Состояние

- Oversized implementation (~1670 lines) was opened as PR #59 and **closed** (over the ≤400 line rule). Commits remain on `cursor/capability-21-external-mas-fecb` for cherry-pick reference only — **do not reopen as one PR**.
- Base for new work: `main` @ `2ac0d5b` (or current `main` after fetch).
- This handoff PR is the single open PR; next agent starts **PR1** below from `main`.

## Сделано

Reference branch `cursor/capability-21-external-mas-fecb` (closed PR #59) already sketched:

| Piece | Path (on that branch) | Notes |
|---|---|---|
| Config + errors | `coscientist/integrations/config.py`, `errors.py` | `ExternalMasConfig.enabled_integrations`; all fields required |
| Ports | `coscientist/integrations/ports.py` | `StudyCreatePort`, `JournalStreamPort`, `ModuleHandler`, `A2AHttpTransport` |
| A2A facade | `coscientist/integrations/a2a_facade.py` | agent card / create_task / stream_events |
| API wiring | `coscientist/api/a2a.py` | `PostStudiesCreatePort`, `StoreJournalStreamPort` |
| Remote Tool | `coscientist/tools/integrations/a2a_remote.py` | `provider=http` on `ToolSpec` |
| MCP server | `coscientist/integrations/mcp_server.py`, `api/mcp_facade.py` | JSON-RPC initialize / tools/list / tools/call |
| Tests | `tests/integrations/*` | fakes only |

Treat that branch as a **source to split**, not as mergeable unit. Prefer re-implementing thin slices over cherry-picking the whole tree (avoids shims and orphan import-linter list churn).

## Следующий PR

**PR1 (≤400 lines, base=`main`, stack parent=none):** ports + errors + minimal config + A2A facade + facade unit tests (fakes).

Suggested file budget (keep total diff ≤400 including tests):

1. `coscientist/integrations/__init__.py` — re-exports only what PR1 needs.
2. `coscientist/integrations/errors.py` — `IntegrationError`, `IntegrationDisabled`, …
3. `coscientist/integrations/ports.py` — `JournalEventView`, `StudyCreatePort`, `JournalStreamPort` (defer `ModuleHandler` / `A2AHttpTransport` to later PRs if line budget tight).
4. `coscientist/integrations/config.py` — **only** `ExternalMasConfig` + `A2AAgentCardConfig` + `A2AFacadeConfig` (leave remote/MCP configs out).
5. `coscientist/integrations/a2a_facade.py` — `A2AFacade` (`a2a` gate, agent_card, create_task, list/stream_events).
6. `tests/integrations/test_a2a_facade.py` — disabled refuse, create mapping, stream after seq.
7. `CHANGELOG.md` — short Unreleased bullet.
8. `pyproject.toml` — add `coscientist.integrations` to core-forbidden + new contracts (`integrations` ↛ core/mcp/httpx/api/tools); extend ingest/lifecycle/`_attempt_frame` source lists **only for modules this PR adds**.

**Do not** in PR1: `api/a2a.py`, remote Tool, MCP server, `provider=http`, full config surface.

**PR2 (≤400, base=PR1 head or `main` after PR1 merges; stack ≤2):** `api/a2a.py` adapters + one wiring test against `post_studies` + fake journal store; import-linter source-list entries for `api.a2a`.

**PR3 (after PR1 merged; new stack from `main`):** `A2ARemoteToolConfig` + `tools/integrations/a2a_remote.py` + `ToolProvider` add `"http"` + fake-transport tests.

**PR4:** `McpServerFacadeConfig` + `McpModuleServer` + handler fakes tests (no `coscientist.mcp` import).

**PR5:** `api/mcp_facade.py` + thin wiring test.

Each PR: pytest for touched tests + `audit_heuristics.py --vs-main` + `lint-imports`. Close previous open PR before opening the next (≤1 open).

## Контракты

```text
ExternalMasConfig.enabled_integrations: tuple[str, ...]
  requires("a2a" | "a2a_remote" | "mcp_server") → IntegrationDisabled if absent

StudyCreatePort.create_study(profile, request, catalog_version, idempotency_key) -> study_id
JournalStreamPort.read_after(study_id, seq) -> list[JournalEventView]
A2AFacade.agent_card() / create_task(message) / stream_events(task_id, after_seq)

A2AHttpTransport.request(method, url, headers, body, timeout_s) -> A2ATransportResponse
RemoteA2AAgentTool: ToolSpec.provider == "http"; ToolError on disabled / bad peer

ModuleHandler.invoke(arguments) -> dict
McpModuleServer.handle(json_rpc_message) -> result | error | None(notification)
```

No invented timeouts/status lists/module names — all from config. No httpx outside `mcp.httpx_transport`. No shims that duplicate API or soft-default missing config.

## Вопросы

1. Exact A2A wire field names for task create / result (peer agent card schema version) — keep fully config-driven until a pinned A2A version is chosen.
2. Whether MCP facade pagination (`tools/list` cursor) is ever required; current design: typed refuse if cursor present.
3. Whether `provider=http` stays on `ToolSpec` or becomes a separate registry class when ToolRouter lands.

## Проверки

Per PR:

```bash
python -m pytest tests/integrations/ -q   # or the PR's new test file(s)
python scripts/audit_heuristics.py --vs-main
lint-imports
# line budget
git diff --numstat origin/main...HEAD | awk '{a+=$1;d+=$2} END{print a+d}'  # must be ≤400
```

Do **not** wait on GitHub CI unless asked; run locally once before push.

## Риски

- **Line budget:** packing config+facade+API+MCP+tests in one PR will again exceed 400 — split as above.
- **import-linter source lists:** three contracts require every new module listed; forget → `test_ingest_gate_contracts` fails.
- **audit `field_default`:** avoid `x: T | None = None` AnnAssign in method bodies; optional Pydantic fields need hand `audit-ok[field_default]` with a concrete reason.
- **Shim temptation:** do not add “temporary” wrappers that call stubs; wire real `post_studies` / fakes only.
- **Parallel agents:** agent layer owns MCP *client* + roles; this track owns facades/Protocols only — do not edit `coscientist/agents/` or thicken `coscientist/mcp/` client here.
