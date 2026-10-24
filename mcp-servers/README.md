# MCP servers (M1 literature live)

`docker-compose.yml` builds `papers-search` (:7331) and `paper-analysis` (:7334) from a
[CoResearchGraph](https://github.com/aimclub/CoResearchGraph) checkout (`mcp-servers/`).

```bash
export COSCIENTIST_DEV_ROOT=/path/to/CoResearchGraph
cp "$COSCIENTIST_DEV_ROOT/mcp-servers/papers-search-mcp-server/.env.example" mcp-servers/papers-search.env
cp "$COSCIENTIST_DEV_ROOT/mcp-servers/paper-analysis-mcp-server/.env.example" mcp-servers/paper-analysis.env
# fill secrets — coscientist has no silent defaults
docker compose -f mcp-servers/docker-compose.yml up --build
```

Wire URLs explicitly via `coscientist.tools.mcp_live.literature_live_bundle` → `open_tool_router`
(depends on ToolRouter #139). CI uses FakeStdio in `tests/tools/test_mcp_live.py`.
