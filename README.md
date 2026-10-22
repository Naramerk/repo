# CoScientist-next

From-scratch rewrite of CoScientist. The core owns study state; LLMs only produce typed data.

Design: [`docs/DESIGN.md`](docs/DESIGN.md) · Capabilities: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md)

## Quickstart (M0)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q -m "not slow"
lint-imports
```

M0 is the pure state core: domain + generic Node/Edge kind registry, SQLite WAL `EventStore` (lease/epoch fencing, write queue, projections, upcasters), `compute_outcome` / `Executor.record`, requirement catalog (`text_ru` + machine check), deadline timers with injectable clock, content-addressed artifacts.

No LLM, ADK, or web in this milestone. `coscientist.core` must not import `google.adk`, `httpx`, or web packages (enforced by import-linter).

## Layout

```
coscientist/core/     state machine, store, executor
coscientist/checks/   catalog + CodeCheck evaluation
tests/                M0 unit, property, and load tests
docs/                 DESIGN.md, CAPABILITIES.md
```
