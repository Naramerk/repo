# GLM live-run recipe (first live end-to-end `coscientist run`)

Observed outcome (2026-10-25, `glm-smoke-3`): **exit 0, verdict `fulfilled`** —
tz_spec → planner → executor-route (`static:ping` cassette tool called,
`{"pong":true}` recorded), study closed. Usage per role: tz_spec 806+8176 tok,
planner 1160+1028 tok, executor 444+583 tok (glm-5.3 via Z.ai).

## Run

```bash
source ~/.config/cosnext/env   # GLM_API_KEY
bash bench/lab/glm/run.sh      # or COSCI_GLM_STUDY_ID=... bash run.sh
```

## Model choice

glm-4.7 only (policy 2026-10-26: cheap fast models for all benchmark runs;
large models forbidden in runs). The gateway serves glm-4.7 as a flash variant
which ignores strict json_schema response_format — the three adapter
accommodations below make it pass (live-verified fulfilled, ~5x faster and
cheaper than glm-5.3).

## Findings baked into the branch

1. Providers may ignore `response_format` — the adapter repeats the response
   JSON Schema in the system message; acceptance stays `model_validate_json`.
2. Providers cannot always nest escaped JSON inside a JSON string (glm-5.3
   emitted `` {`pong`: true} `` with backticks) — `ToolArg.value_json` accepts
   a parsed value (before-validator canonicalizes to JSON text); wire contract
   unchanged.
3. One bounded schema-repair retry feeds the validation error back once
   (budget-checked, counted; no gate weakened).
