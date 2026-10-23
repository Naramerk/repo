# CoScientist-next

From-scratch rewrite of CoScientist. The core owns study state; LLMs only produce typed data.

Design: [`docs/DESIGN.md`](docs/DESIGN.md) · Capabilities: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) · Research graph: [`docs/RESEARCH_MODEL.md`](docs/RESEARCH_MODEL.md) (kinds register into the core graph; not hard-coded in M0)

## Quickstart (M0)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q -m "not slow"
lint-imports
```

M0 is the pure state core: domain + generic Node/Edge kind registry, SQLite WAL `EventStore` (lease/epoch fencing, write queue, projections, upcasters), `compute_outcome` / `Executor.record`, deadline timers with injectable clock, content-addressed artifacts.

No LLM, ADK, or web in this milestone. `coscientist.core` must not import `google.adk`, `httpx`, or web packages (enforced by import-linter).

## Layout

```
coscientist/core/          M0 state-core (no HITL: tick/view/recover ignore or refuse gates)
coscientist/checks/        M1 requirement catalog + CodeCheck evaluation
coscientist/experimental/  pre-M2/M5: HITL gates, DelegatedBudget/costly_gate, report stub
coscientist/schema/        graph_schema.yaml (packaged)
tests/                     dual runtime_kind=[core,experimental]; pure HITL under experimental/
docs/                      DESIGN.md, CAPABILITIES.md
```


