# Lab: live MCP servers (internal network, VPN)

Real internal MCP servers wired into the `coscientist run` live-MCP path
(`--mcp-tool-bundle` + `--tool-call-args`; see `docs/cli-run-tools.md` § Live MCP).
All URLs are reachable only from the user VPN. Verified 2026-10-25.

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

## Known gaps / risks

- **CLI JSON loading fix (this branch):** strict tuple fields rejected JSON arrays when the
  bundle was loaded via `json.loads` + `model_validate`; `load_mcp_tool_router_bundle` now
  validates JSON *text* through pydantic's JSON parser (same strict model) and
  `coscientist/cli/run.py` passes the file text. Regression test:
  `tests/tools/test_mcp_live.py::test_load_bundle_from_json_text_accepts_arrays`.
- **`literature_live_bundle` drift:** the real `paper-analysis` server now lists 5 tools
  (adds `get_papers_database_statistics`), but `PAPER_ANALYSIS_TOOLS` in
  `coscientist/tools/mcp_live.py` requires exactly 4 → `literature_live_bundle` would
  refuse against the live server. Needs a profile update (all 5 tools) before the
  literature bundle can open; not changed here (no literature tools/call was made).
- No literature bundle file yet: literature servers were probed for discovery only
  (initialize + tools/list, exit 0). Build the literature bundle after the
  `PAPER_ANALYSIS_TOOLS` update, using `probe.py` to re-verify.
- `http_request_ok_statuses` is `[200]`; the server may return SSE (`text/event-stream`)
  on the same 200 — handled by both the repo transport and `probe.py`.
- Internal IPs are stable as of today but VPN-only; URLs belong to the user's environment,
  not this repo's CI.
