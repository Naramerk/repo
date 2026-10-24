# Old vs new CoScientist benchmark harness

Package: [`bench/`](../bench/). Compares the reference (old) system and
CoScientist-next on the same versioned task set.

## Metrics

| Metric | Source |
|--------|--------|
| Wall time | Harness `time.perf_counter` around `BenchRunner.run_task` |
| Tokens | Provider usage only (`ProviderTokenUsage`); never estimated |
| LLM / tool calls | Runner instrumentation (`CallCounts`) |
| Quality | Exact match of reported report fields + plan criterion statuses against task YAML |

Missing provider usage → runner must return `RunnerRefuse(reason_code="tokens_unavailable")`.

## Task set

Tasks live under `bench/tasks/<version>/` (manifest + one YAML per task). Nothing
in the harness hardcodes prompts or task ids.

```bash
python -m bench run \
  --manifest bench/tasks/v1/manifest.yaml \
  --out /tmp/bench-out \
  --runners fake
```

## Runner Protocol

Systems integrate through `bench.protocol.BenchRunner`:

```python
async def run_task(self, task: BenchTask) -> RunnerSuccess | RunnerRefuse: ...
```

The new agent/MCP layer (built in parallel) should expose a callable with that
shape and wire it via `NewCoScientistRunner(..., injected=...)` or
`NewRunnerConfig.entrypoint = "module:attr"`.

## Pointing at the old system (local checkout)

Upstream reference: **aimclub/CoScientist_dev** branch **dev_1**. If that repo
is private or unreachable from CI, clone it locally and configure an explicit
entrypoint (no path/name heuristics):

1. Clone and check out `dev_1`.
2. Add a small adapter module in that checkout (or on `PYTHONPATH`) that
   implements `async def run_task(task: BenchTask) -> RunnerSuccess | RunnerRefuse`
   and fills `ProviderTokenUsage` from the old system’s provider usage records.
3. Copy `bench/runners.example.yaml` and set:

```yaml
old:
  system_id: old
  checkout_path: /absolute/path/to/CoScientist_dev
  expected_branch: dev_1
  verify_branch: true
  entrypoint: your_adapter_module:run_task
new:
  system_id: new
  entrypoint: null   # or module:attr once the new agent runner exists
```

4. Run:

```bash
python -m bench run \
  --manifest bench/tasks/v1/manifest.yaml \
  --out /tmp/bench-out \
  --runners config \
  --runners-config path/to/runners.yaml
```

`checkout_path` must be an existing directory; `entrypoint` must be exactly
`module:attr`. Misconfiguration yields a typed `RunnerRefuse` /
`BenchAdapterError`, not a silent fallback.

## Outputs

Under `--out`:

- `result.json` — full `BenchSuiteResult`
- `summary.md` — per-task old vs new metrics
