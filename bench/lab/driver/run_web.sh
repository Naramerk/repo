#!/usr/bin/env bash
# bench/lab/driver/run_web.sh — web-lane end-to-end: uvicorn API + webdrive.
#
# Launches the study API (python -m coscientist.api.serve) and drives one
# study through the web lane (POST /studies/{id}/run + WS stream) with
# bench/lab/driver/webdrive.py against a task file. All dependency additions
# (uvicorn, websockets, httpx) come from `uv --with` at runtime — the repo
# dependency surface is unchanged.
#
# Usage:
#   run_web.sh TASK_FILE STUDY_ID CONFIG_JSON DATA_ROOT CATALOG_JSON PORT
# Example config JSON (POST /run body minus task_text):
#   {
#     "owner": "web-lane", "deadline_s": 3600.0, "lease_ttl_s": 600.0,
#     "catalog_version": 4, "catalog_path": "bench/lab/glm/checks_catalog.json",
#     "profile": "research-glm", "tool_catalog_version": 1,
#     "llm_config_path": "bench/lab/glm/llm.json",
#     "planner_model": "glm-5.3", "executor_model": "glm-5.3", "tz_spec_model": "glm-5.3",
#     "tools_json_path": "bench/lab/glm/tools.json",
#     "mcp_tool_bundle_path": null, "tool_call_args_path": null,
#     "coder_backend": null, "coder_jobs_path": null,
#     "wired_roles": [], "task_input_paths": [],
#     "require_hypotheses": false, "confirmations_needed": 1,
#     "refutations_needed": 1, "refutation_priority": false,
#     "supported_needed": 2, "max_expansions": 0, "finalize_reserve_s": 60,
#     "rng_seed": 0, "max_llm_calls": 60, "max_tokens": 400000,
#     "max_tool_calls": 10, "max_ticks": 40, "hitl_mode": "headless"
#   }
set -euo pipefail
cd "$(dirname "$0")/../../.."

if [ "$#" -ne 6 ]; then
  echo "usage: $0 TASK_FILE STUDY_ID CONFIG_JSON DATA_ROOT CATALOG_JSON PORT" >&2
  exit 2
fi

TASK_FILE="$1"
STUDY_ID="$2"
CONFIG_JSON="$3"
DATA_ROOT="$4"
CATALOG_JSON="$5"
PORT="$6"
RUN_DIR="$(dirname "$TASK_FILE")/webdrive_out"
EVENTS_OUT="$RUN_DIR/${STUDY_ID}_events.jsonl"
SUMMARY_OUT="$RUN_DIR/${STUDY_ID}_summary.json"
mkdir -p "$RUN_DIR" "$DATA_ROOT"

# API server (uvicorn + websockets via uv --with; explicit args, no defaults).
uv run --extra api --with uvicorn --with websockets python -m coscientist.api.serve \
  --data-root "$DATA_ROOT" \
  --catalog "$CATALOG_JSON" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --stream-poll-interval-s 0.1 \
  --run-wait-s 30 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Driver (httpx + websockets via uv --with). --connect-retries tolerates
# server startup; timeout bounds the whole status+final collection.
uv run --with httpx --with websockets python bench/lab/driver/webdrive.py \
  --base-url "http://127.0.0.1:$PORT" \
  --study-id "$STUDY_ID" \
  --task-file "$TASK_FILE" \
  --config "$CONFIG_JSON" \
  --timeout-s "${WEBDRIVE_TIMEOUT_S:-900}" \
  --events-out "$EVENTS_OUT" \
  --summary-out "$SUMMARY_OUT" \
  --connect-retries 120
