# Plan: profiles medicine (#12) + microfluidics (#13)

Area: `coscientist/profiles/` (medicine + microfluidics). Capabilites from
`docs/CAPABILITIES.md` #12 / #13; DESIGN §8. Old code
(`aimclub/CoScientist` `system.yaml` MedicalAgent + `tools/med_tools.py`;
`microfluidics/models.py` + `agents/microfluidics.yaml`) is behaviour reference
only — not ported.

## Hard rules (every PR)

- Profiles = **versioned config** (YAML/JSON schema). No domain heuristics or
  profile branching hardcoded in `core/` / `agents/`.
- Tools exposed only via the **MCP catalog** (Protocol + fakes in unit tests).
- Depend on **narrow Protocols**; agent layer / MCP client / planner / research
  cycle land in parallel — do not implement them here.
- Unknown / invalid config → **typed refuse**, never guess.
- Tests for every new public function + each acceptance criterion touched.
- Local gate before push: `pytest` + perf (`tests/test_load_writes.py`) +
  `scripts/audit_heuristics.py --vs-main`. Do **not** wait for / re-run GitHub CI.
- Never merge; open each PR as soon as ready. Stack on `main` when independent;
  stack on a prior area PR when the slice needs its types/ports.

## Acceptance (done when all green)

| # | Criterion | Owning PR |
|---|---|---|
| 12 | profile `medicine`: route `medical` executes a task with `med_tools` | P17 |
| 13a | profile `microfluidics` starts; study **root** created by core | P20 |
| 13b | profile `microfluidics` executes the reference task | P21 |
| R1 | every shipped profile starts and has study root | P22 |

## Planned PRs

Stack note: `main` = independent of other area PRs. `← Pn` = open stacked on
that PR’s branch (or rebase onto it after it lands on main).

| PR | Title | Scope (1 line) | Depends |
|---|---|---|---|
| **P1** | docs: profiles medicine/microfluidics PR plan | This plan file under `docs/plan/`. | main |
| **P2** | profiles: package skeleton + import-linter | Empty `coscientist/profiles/` (+ `__init__.py`); import-linter: profiles ↛ api/web; core ↛ profiles. | main |
| **P3** | profiles: Profile domain models | Frozen pydantic `Profile`, `RoleConfig`, `RouteConfig` (DESIGN §8); all operational fields required (no guessed defaults). | ← P2 |
| **P4** | profiles: ProfileError typed refuse | `ProfileError` / `ProfileRefuseKind` (unknown_profile, invalid_config, missing_route, missing_tools); no stringly fallbacks. | ← P3 |
| **P5** | profiles: ProfileLoader Protocol | Narrow `ProfileLoader` Protocol: `load(name) -> Profile`; tests with a fake loader only. | ← P4 |
| **P6** | profiles: YAML ProfileLoader | Load packaged `<name>/profile.yaml` + `profile_version`; invalid YAML → typed refuse; digest = sha256(JCS(config)). | ← P5 |
| **P7** | profiles: ToolCatalog Protocol | `ToolCatalog` Protocol: `list_tools(profile) -> tuple[ToolSpecName, ...]`, `has_tool(name)`; no MCP I/O yet. | ← P3 |
| **P8** | profiles: FakeToolCatalog | In-memory fake for unit tests (register by name; exact membership). | ← P7 |
| **P9** | profiles: McpCatalogPort adapter | Adapter Protocol over MCP `list_tools` surface (injectable session); maps `McpTool` → catalog entries; fake session in tests. | ← P7 |
| **P10** | profiles: ProfilePlugin + Registry Protocols | DESIGN §8 `ProfilePlugin.register(Registry)` — tools / check kinds / templates / routes only; no state-machine hooks. | ← P3 |
| **P11** | profiles: medicine profile.yaml | Versioned config only: `name=medicine`, route `medical`, role + `tools` listing med tool names, `mcp_servers` stub entries, `alembic`. | ← P6 |
| **P12** | profiles: medicine med_tools catalog entries | Config declaring MCP tool names matching reference `MedToolset`: `search_pubmed`, `get_pico`, `get_study_taxonomy`, `analyze_medical_image` (prefix policy from config). | ← P11 |
| **P13** | profiles: FakeMedMcpServer | Test fake MCP server exposing the four med tools (deterministic payloads); no network / PubMed / VLM. | ← P9, P12 |
| **P14** | profiles: RouteRunner Protocol | Narrow `RouteRunner.run(route, task, catalog) -> AgentTaskOutput` (or area-local typed out); fake runner for tests. | ← P7 |
| **P15** | profiles: medicine RouteRunner fake path | Fake runner: given route=`medical` and catalog containing med tools, records a successful tool call + output (no LLM). | ← P14, P13 |
| **P16** | profiles: medicine load + resolve medical route | `load("medicine")` → Profile with route `medical` whose `tools` ⊆ catalog; missing tool → typed refuse. | ← P11, P8 |
| **P17** | profiles: #12 acceptance — medical + med_tools | Integration-style unit test: create study `profile=medicine`, accept plan with `route=medical`, fake runner invokes med tool from catalog, task DONE(SUCCESS). | ← P15, P16 |
| **P18** | profiles: microfluidics profile.yaml | Versioned config: `name=microfluidics`, routes for reference task execution, mcp_servers, alembic; no agent code. | ← P6 |
| **P19** | profiles: microfluidics reference task config | Packaged reference task fixture (key/goal/route/tools_hint) aligned with old LIT-style reference (`LiteratureQuery` shape as config, not ported models). | ← P18 |
| **P20** | profiles: #13a start — study root by core | Test: `create_study(profile="microfluidics")` succeeds; journal has `StudyCreated` + `GraphNodeUpserted(kind=study)` from core (no profile code creates the root). | ← P18 |
| **P21** | profiles: #13b execute reference task | Test: with microfluidics profile + fake runner/catalog, reference task from P19 runs to DONE(SUCCESS). | ← P19, P14, P20 |
| **P22** | profiles: R1 every profile has root | `test_r1_every_profile_starts_and_has_root` parametrize `medicine`, `microfluidics` (and any other packaged profile names from loader). | ← P17, P21 |
| **P23** | profiles: package data force-include | Hatch `force-include` for `profiles/*/profile.yaml` (+ reference fixtures); wheel install smoke test. | ← P11, P18 |
| **P24** | profiles: unknown profile refuse at boundary | API/intake boundary helper: unknown profile name → `ProfileRefuseKind.unknown_profile` (typed); no defaulting to `research`. | ← P4, P6 |
| **P25** | profiles: catalog_extensions hooks (noop wiring) | Profile field `catalog_extensions: list[str]` validated against registered template ids via Protocol; empty OK; unknown id → refuse. | ← P10 |
| **P26** | profiles: CHANGELOG + CAPABILITIES checkmarks note | Document #12/#13 done in CHANGELOG once P17+P21+P22 green; no behaviour change. | ← P22 |

## Out of scope (other agents)

- Agent layer / LLM roles / MCP client internals / planner / critic / research
  cycle / HITL UI / reporting / alembic / coder jobs.
- Porting ADK `MedToolset` implementations or PubMed/VLM clients — only
  **names + schemas in config** and **fakes** for acceptance.
- Porting microfluidics `StructuredTZ` agent pipeline / VibePAV HITL — only
  profile start + reference task execution via Protocols.
- `research` profile fullness (#2/#3) — sibling agents.

## Reference map (behaviour only)

| Old | New slice |
|---|---|
| `system.yaml` MedicalAgent (`route`/tools: medical) | P11–P17 |
| `tools/med_tools.py` MedToolset (4 tools) | P12–P13 (names + fake) |
| `agents/microfluidics.yaml` root InitAgent | P18, P20 (start via core) |
| `microfluidics/models.py` LiteratureQuery | P19 (config fixture shape) |

## Local verification (each PR)

```bash
pytest -q -m "not slow"
pytest -q tests/test_load_writes.py -m perf
python scripts/audit_heuristics.py --vs-main
```
