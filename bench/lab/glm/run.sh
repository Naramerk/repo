#!/usr/bin/env bash
# GLM live end-to-end study run (bench/lab/glm/README.md for the observed outcome).
#
# Requires: ~/.config/cosnext/env with GLM_API_KEY (and optionally GLM_BASE_URL),
# network access to https://api.z.ai/api/paas/v4, and `uv` in PATH.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${COSNEXT_ENV_FILE:-$HOME/.config/cosnext/env}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
: "${GLM_API_KEY:?GLM_API_KEY must be set (source ~/.config/cosnext/env)}"

RUN_DIR="${COSCI_GLM_RUN_DIR:-run}"
STUDY_ID="${COSCI_GLM_STUDY_ID:-glm-live-run-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$RUN_DIR/data"

uv run coscientist run task.txt \
  --data-root "$RUN_DIR/data" \
  --deadline-s 1200 \
  --catalog checks_catalog.json \
  --catalog-version 4 \
  --profile research-glm \
  --tool-catalog-version 1 \
  --owner glm-live-run \
  --llm-config llm.json \
  --planner-model glm-5.3 \
  --executor-model glm-5.3 \
  --tz-spec-model glm-5.3 \
  --tools-json tools.json \
  --study-id "$STUDY_ID" \
  --require-hypotheses false \
  --confirmations-needed 1 \
  --refutations-needed 1 \
  --refutation-priority false \
  --supported-needed 2 \
  --max-expansions 0 \
  --finalize-reserve-s 60 \
  --lease-ttl-s 600 \
  --rng-seed 0 \
  --max-llm-calls 60 \
  --max-tokens 400000 \
  --max-tool-calls 10 \
  --max-ticks 40 \
  --hitl-mode headless
