# `coscientist run` — cassette vs live ToolRouter

CLI study runs require an explicit tool path. Exactly one of:

| Flag | Mode | Binding |
|------|------|---------|
| `--tools-json PATH` | Cassette (hermetic) | `StaticToolRouter` — fixed result/args map; no MCP I/O |
| `--mcp-tool-bundle PATH` | Live MCP | Real `ToolRouter.open` via `McpToolRouterBundle` |

## Cassette (`--tools-json`)

JSON object: bare tool name → `{"result": object, "args": object}` (both keys required; `args` may be `{}`).

Public tool ids / planner `tools_hint` are exact `static:<name>` (same `server:tool` rule as MCP). Unknown hints refuse. Args must match the cassette exactly — CLI never invents defaults.

Example:

```bash
coscientist run task.txt \
  --tools-json cassette/tools.json \
  --llm-config llm.json \
  --catalog catalog.json --catalog-version 1 \
  --profile research --owner me --study-id demo \
  --planner-model m --executor-model m --tz-spec-model m \
  --require-hypotheses false --hitl-mode headless \
  --data-root ./data --deadline-s 3600 \
  --finalize-reserve-s 30 --lease-ttl-s 60 --rng-seed 0 \
  --max-llm-calls 100 --max-tokens 1000000 --max-tool-calls 50 --max-ticks 50
```

## Live MCP (`--mcp-tool-bundle` + `--tool-call-args`)

`--mcp-tool-bundle` is a JSON `McpToolRouterBundle`: `{ "client": <McpClientConfig>, "router": <ToolRouterConfig> }`.

- Client and router **server names must match**.
- Every tool must be listed in the router config (no discovery, no blanket-allow).
- CLI opens the router through `open_tool_router_for_cli` (stdio refuse-fetch, or `HttpxTransport` when any streamable HTTP server is present). Requires the `mcp` extra when HTTP servers are used.

`--tool-call-args` is required: JSON object of exact `server:tool` → args object (`{}` allowed). Keys that are not bound refuse. The CLI allow-list is the declared-args set (not every MCP-bound tool). Planner `tools_hint` must be those exact `server:tool` names; missing call-args refuse with `CliRefuse` before `start_attempt` (no dangling `AttemptStarted`).

```bash
coscientist run task.txt \
  --mcp-tool-bundle live/bundle.json \
  --tool-call-args live/call_args.json \
  ... # same required study/budget/--hitl-mode flags as cassette
```

## Optional roles (`--wired-roles`)

JSON array: `["hypothesizer","researcher","report"]` (default `[]`).

| Planner Need / stage | Unwired (default) | Wired |
|----------------------|-------------------|--------|
| HYP/LIT Need off | `cancel_task` → SKIPPED (not fabricated SUCCESS) | same |
| HYP/LIT Need on | `CliRefuse` | `CliRefuse` before `start_attempt`: adapter schema gate, then CLI journal-record gate (no dangling `AttemptStarted`, no stub SUCCESS) |
| REPORT | cancel (not wired) | Same dual gate — no stub SUCCESS / no open attempt without record |

Today `LlmAgentAdapter` only schemas `tz_spec` / `planner` / `executor-route`. Declaring optional roles in `--wired-roles` is the CLI seam; science for LIT/HYP/REPORT still depends on adapter schemas + CLI journal record + catalog/profile (#65) and sandbox (#150). Do not invent tools or fake stage SUCCESS.

## Budgets / HITL / CallCtx

Unchanged: cumulative adapter budgets, required `--hitl-mode`, and `CallCtx` on live `ToolRouter.call` (study/task/attempt + study `HitlPolicy`).
