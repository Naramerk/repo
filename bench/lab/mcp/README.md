# Lab: live MCP servers (internal network, VPN)

Real internal MCP servers wired into the `coscientist run` live-MCP path
(`--mcp-tool-bundle` + `--tool-call-args`; see `docs/cli-run-tools.md` § Live MCP).
All URLs are reachable only from the user VPN. Verified 2026-10-25 (heracleum-tox)
and 2026-10-26 (papers-search, paper-analysis).

## Servers

| Server | URL | serverInfo | Tools (exact names from `tools/list`) |
|--------|-----|------------|----------------------------------------|
| `heracleum-tox` | `http://10.32.11.45:7336/mcp` | HeracleumTox 4.0.5 (protocol 2025-06-18) | `dataset_overview_heracleum_tox`, `chemical_space_clustering`, `predict_ld50`, `predict_general_toxicity`, `applicability_domain`, `estimate_synthesis_cost`, `predict_molecule_profile`, `model_quality`, `reproduce_all`, `reproduce_claims` (10) |
| `papers-search` | `http://10.32.11.45:7331/mcp` | fastmcp | `search_entity`, `search_papers`, `download_papers_from_search` (3) |
| `paper-analysis` | `http://10.32.11.45:7334/mcp` | fastmcp | `explore_scientific_database`, `explore_my_papers`, `find_papers_in_db`, `find_relevant_data_in_db`, `get_papers_database_statistics` (5) |

Literature URLs come from the legacy repo env (`MCP__PAPERS_SEARCH_URL`,
`MCP__PAPER_ANALYSIS_URL` in `/Users/nargiza/CoScientist_dev-dev_1-2026-10-22/.env`).

### heracleum-tox tool arg schemas (verified live)

| Tool | Args |
|------|------|
| `dataset_overview_heracleum_tox` | `{}` (no properties) |
| `chemical_space_clustering` | `{"n_clusters": int}` (optional, default 5) |
| `estimate_synthesis_cost` | `{"name_or_smiles": str}` (required; name e.g. `xanthotoxin` or SMILES) |
| `predict_ld50` | `{}` |
| `predict_general_toxicity` | `{}` |
| `applicability_domain` | `{"endpoint": str}` (optional; `ld50\|hepatotoxicity\|dili\|cardiotoxicity\|carcinogenicity`, default `ld50`) |
| `predict_molecule_profile` | `{"name_or_smiles": str}` (required) |
| `model_quality` | `{}` |
| `reproduce_all` | `{}` — trains models; first call slow |
| `reproduce_claims` | `{}` |

### literature tool arg schemas (papers-search + paper-analysis, verified live 2026-10-26)

| Server | Tool | Args (from `tools/list` inputSchema) |
|--------|------|--------------------------------------|
| papers-search | `search_entity` | `entity_type`, `entity_name` (required); optional `email`, `api_key` |
| papers-search | `search_papers` | all optional: `keywords`, `author_id`, `institution_id`, `source_id`, `publication_year`, `open_access` (default true), `has_pdf` (default true), `limit` (default 10), `sort`, `email`, `api_key` |
| papers-search | `download_papers_from_search` | same filters as `search_papers` plus `session_id`/`user_id` (default `"1"`); uploads found PDFs to S3 |
| paper-analysis | `explore_scientific_database` | `task` (required); optional `top_k` (60), `rerank_k` (20) |
| paper-analysis | `explore_my_papers` | `task`, `s3_keys` (both required) |
| paper-analysis | `find_papers_in_db` | `task` (required); optional `initial_number_of_chunks` (60), `number_of_chunks_after_rerank` (20) |
| paper-analysis | `find_relevant_data_in_db` | `task` (required); optional `search_images` (false), `top_k` (60), `rerank_k` (20) |
| paper-analysis | `get_papers_database_statistics` | `{}` (no properties) |

## Scenario → server mapping

| Scenario | Server | URL | Tools used |
|----------|--------|-----|-----------|
| `two_tasks` | heracleum-tox | `http://10.32.11.45:7336/mcp` | `dataset_overview_heracleum_tox`, `chemical_space_clustering`, `estimate_synthesis_cost` (wired in `heracleum_call_args.json`; full 10-tool surface bound in the bundle) |
| `article_small` | heracleum-tox | `http://10.32.11.45:7336/mcp` | same as `two_tasks` |
| `web` | heracleum-tox | `http://10.32.11.45:7336/mcp` | same as `two_tasks` |
| `recovery` | heracleum-tox | `http://10.32.11.45:7336/mcp` | same as `two_tasks` |
| `recovery2` | heracleum-tox | `http://10.32.11.45:7336/mcp` | same as `two_tasks` |
| `borshevik` | papers-search + paper-analysis | `http://10.32.11.45:7331/mcp`, `http://10.32.11.45:7334/mcp` | `search_entity`, `search_papers`, `download_papers_from_search`; `explore_scientific_database`, `explore_my_papers`, `find_papers_in_db`, `find_relevant_data_in_db`, `get_papers_database_statistics` |
| `microfluidics` | papers-search + paper-analysis | `http://10.32.11.45:7331/mcp`, `http://10.32.11.45:7334/mcp` | same literature tools as `borshevik` |

## Files

- `heracleum_bundle.json` — `McpToolRouterBundle` (`{client, router}`) for `heracleum-tox`.
  Binds **all 10** listed tools (required: `_require_bindings` enforces exact set equality
  between router bindings and server `tools/list`; a subset bundle refuses at open).
- `heracleum_call_args.json` — `--tool-call-args` map: exact `server:tool` → args for the
  three verified tools (`{}`, `{"n_clusters": 5}`, `{"name_or_smiles": "xanthotoxin"}`).
  The CLI allow-list is this declared set, not all bound tools.
- `heracleum_dataset_overview_sample.json` — recorded live `tools/call` response for
  `dataset_overview_heracleum_tox`; confirms the legacy shape
  `{"answer": {"n_reconstructed": 225, "by_chemical_class": {"fatty_acid": 132, ...}}}`.
- `literature_bundle.json` — `McpToolRouterBundle` binding **both** literature servers
  (`papers-search` + `paper-analysis`; client and router names match). Lists **all 8**
  tools (3 + 5): `ToolRouter.open` → `_require_bindings` enforces exact set equality
  against each server's `tools/list` (a subset bundle refuses at open).
- `literature_call_args.json` — `--tool-call-args` map for the wired LIT stage: exactly
  the three `server:tool` ids the researcher role resolves
  (`coscientist/profiles/packaged/research-glm.v1.yaml` roles.researcher.tools →
  `papers-search:search_papers`, `papers-search:download_papers_from_search`,
  `paper-analysis:find_papers_in_db`). The CLI allow-list is this declared set, not all
  bound tools; edit the keyword/task values per study topic (they are the planned args
  the researcher executes).
- `literature_sample.json` — recorded live router `tools/call` responses (one per
  server, 2026-10-26): `search_papers` (OpenAlex xanthotoxin hits with DOIs/pdf_url)
  and `get_papers_database_statistics` (12 724 papers, 366 117 chunks).
- `probe.py` — reusable smoke probe (httpx only, the repo `mcp` extra):
  initialize + `tools/list` + optional one `tools/call`, printing raw results.

## Usage

```bash
# discovery / smoke (initialize + tools/list)
uv run --extra mcp python bench/lab/mcp/probe.py http://10.32.11.45:7336/mcp

# one real call
uv run --extra mcp python bench/lab/mcp/probe.py http://10.32.11.45:7336/mcp \
  --call estimate_synthesis_cost --args-json '{"name_or_smiles": "xanthotoxin"}'

# live run path
coscientist run task.txt \
  --mcp-tool-bundle bench/lab/mcp/heracleum_bundle.json \
  --tool-call-args bench/lab/mcp/heracleum_call_args.json \
  ...  # same required flags as docs/cli-run-tools.md
```

### LIT run recipe (wired researcher role, both literature servers)

```bash
coscientist run task.txt \
  --mcp-tool-bundle bench/lab/mcp/literature_bundle.json \
  --tool-call-args bench/lab/mcp/literature_call_args.json \
  --profile research-glm \
  --wired-roles '["researcher"]' \
  ...  # same required flags as bench/lab/glm/run.sh (llm-config, catalog, budgets, hitl-mode)
```

`--wired-roles researcher` requires the profile researcher tools to resolve on the live
surface (`research-glm.v1.yaml`: `search_papers`, `download_papers_from_search`,
`find_papers_in_db`) and each to have declared call args — `literature_call_args.json`
covers exactly those three ids. Planned calls run before `start_attempt`
(`planned_calls`; no invented defaults), and `record_literature` refuses papers that do
not trace to journaled tool results. Edit the `keywords` / `task` values in the
call-args file to the study topic before a real run.

Keep real-server traffic minimal: one call per tool when verifying; `reproduce_all`
trains models (slow) — never call it in smoke checks.

## Verified live (2026-10-25, exit 0)

1. `initialize` → protocol `2025-06-18` negotiated, session id issued.
2. `tools/list` → 10 tools, names above.
3. `tools/call dataset_overview_heracleum_tox {}` → `structuredContent.answer.n_reconstructed == 225`,
   `by_chemical_class.fatty_acid == 132` (legacy shape intact).
4. `tools/call chemical_space_clustering {"n_clusters": 5}` → 5 clusters, family agreement ≈ 0.955.
5. `tools/call estimate_synthesis_cost {"name_or_smiles": "xanthotoxin"}` → `usd_per_g == 311.0` (paper-reported).
6. End-to-end repo path: `load_mcp_tool_router_bundle(json text)` → `open_tool_router_for_cli`
   (HttpxTransport) → 10 tools bound → `LiveCliToolSurface` shows the 3 call-args tools →
   real `router.call` returns `ToolOutput`.

## Verified live — literature bundle (2026-10-26, exit 0)

1. `probe.py` per server: `initialize` + `tools/list` → papers-search 3 tools,
   paper-analysis 5 tools (exact sets = `PAPERS_SEARCH_TOOLS` / `PAPER_ANALYSIS_TOOLS`
   in `coscientist/tools/mcp_live.py`; the exact-5 gate from #226 holds).
2. Offline: `load_mcp_tool_router_bundle(literature_bundle.json)` → client/router names
   align, router routes == exact live sets; `load_profile("research-glm")` researcher
   tools resolve to exactly the 3 `literature_call_args.json` keys.
3. Live repo path: `open_tool_router_for_cli` → 8 tools bound (exact-set gate passed) →
   `LiveCliToolSurface(call_args)` allow-list == the 3 researcher ids → `planned_calls`
   returns `PlannedToolCall` for each.
4. One `router.call` per server: `papers-search:search_papers
   {"keywords": "xanthotoxin", "limit": 3}` → 3 OpenAlex hits (DOIs + pdf_url);
   `paper-analysis:get_papers_database_statistics {}` → 12 724 papers / 366 117 chunks.
   Raw responses: `literature_sample.json`.

## Known gaps / risks

- **`literature_live_bundle` (resolved #226):** `PAPER_ANALYSIS_TOOLS` now requires
  exactly the live 5 tools (adds `get_papers_database_statistics`) — the programmatic
  constructor and this bundle agree with the live servers (verified 2026-10-26 above).
- `download_papers_from_search` uploads found PDFs to S3 (external side effect); the
  bundle flags it `read` like every heracleum tool (route-descriptive — the CLI LIT
  lane dispatches through `ToolRouter.call` and does not gate on `effect`). Never call
  it in smoke checks; it runs only inside a wired LIT stage via planned calls.
- `http_request_ok_statuses` is `[200]`; the server may return SSE (`text/event-stream`)
  on the same 200 — handled by both the repo transport and `probe.py`.
- Internal IPs are stable as of today but VPN-only; URLs belong to the user's environment,
  not this repo's CI.
