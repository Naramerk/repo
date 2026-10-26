#!/usr/bin/env bash
# Recovery lane driver (gap №4, legacy benchmarks recovery/recovery2):
#   run → poll journal for the first ResultRecorded → SIGKILL the process
#   group mid-study → `coscientist continue` on the same journal → checker
#   asserts the study closes fulfilled with unique EXP tasks and no
#   re-executed closed attempts.
#
# Usage: recovery_drive.sh [RUN_DIR]
# Env:   COSCI_RECOVERY_STUDY_ID (default recovery-drive-<ts>)
#        COSCI_RECOVERY_LEASE_TTL_S (default 5 — continue waits out the
#        killed run's lease, exercising the real expiry/takeover path)
#
# Uses only repo assets: the packaged research-glm profile + its
# bench/lab/glm/checks_catalog.json, a local fake LLM (fake_llm.py) and
# static tool cassettes. No network, no real keys (COSCI_TEST_LLM_KEY is
# a dummy consumed by the fake server's llm.json api_key_env).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
cd "$ROOT"

RUN_DIR="${1:-$HERE/out/recovery-$(date +%Y%m%d-%H%M%S)}"
STUDY_ID="${COSCI_RECOVERY_STUDY_ID:-recovery-drive-$(date +%Y%m%d-%H%M%S)}"
LEASE_TTL_S="${COSCI_RECOVERY_LEASE_TTL_S:-5}"
PY="uv run python"
mkdir -p "$RUN_DIR"

PORT="$($PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export COSCI_TEST_LLM_KEY="dummy-key-for-fake-llm"

cleanup() {
  if [ -f "$RUN_DIR/server.pid" ]; then
    kill "$(cat "$RUN_DIR/server.pid")" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "== fixtures =="
$PY "$HERE/fake_llm.py" fixtures --dir "$RUN_DIR" --port "$PORT"

echo "== fake LLM on :$PORT =="
$PY "$HERE/fake_llm.py" serve --dir "$RUN_DIR" --port "$PORT" &
echo $! > "$RUN_DIR/server.pid"
$PY - <<EOF
import sys, time, urllib.request
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:$PORT/healthz", timeout=1)
        sys.exit(0)
    except Exception:
        time.sleep(0.1)
sys.exit("fake llm never became ready")
EOF

# Shared by run and continue (both parsers accept these).
COMMON_ARGS=(
  --catalog "$HERE/../glm/checks_catalog.json" --catalog-version 4
  --profile research-glm --tool-catalog-version 1
  --owner recovery-drive
  --llm-config "$RUN_DIR/llm.json"
  --planner-model glm-4.7 --executor-model glm-4.7 --tz-spec-model glm-4.7
  --tools-json "$RUN_DIR/tools.json"
  --study-id "$STUDY_ID"
  --rng-seed 0 --max-llm-calls 40 --max-tokens 1000000 --max-tool-calls 20
  --max-ticks 20
)
# run-only (study creation surface).
RUN_ONLY_ARGS=(
  --require-hypotheses false --confirmations-needed 1 --refutations-needed 1
  --refutation-priority false --supported-needed 2 --max-expansions 0
  --hitl-mode headless --deadline-s 600 --finalize-reserve-s 30
)

echo "== run (will be SIGKILLed after the first ResultRecorded) =="
$PY "$HERE/fake_llm.py" spawn --pidfile "$RUN_DIR/run.pid" -- \
  uv run coscientist run "$RUN_DIR/task.txt" \
    --data-root "$RUN_DIR/data" --lease-ttl-s "$LEASE_TTL_S" \
    "${RUN_ONLY_ARGS[@]}" "${COMMON_ARGS[@]}" \
    > "$RUN_DIR/run.log" 2>&1 &
DRIVE_WRAPPER=$!

echo "== polling journal for the first ResultRecorded =="
$PY - "$RUN_DIR/data/$STUDY_ID/events.db" <<'EOF'
import sqlite3
import sys
import time

db = sys.argv[1]
deadline = time.time() + 120
while time.time() < deadline:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n = conn.execute(
                "SELECT count(*) FROM events WHERE type='ResultRecorded' "
                "AND entity_key LIKE '%/EXP-%'"
            ).fetchone()[0]
        finally:
            conn.close()
        if n >= 1:
            print(f"EXP ResultRecorded count={n}")
            sys.exit(0)
    except sqlite3.Error:
        pass  # db not created yet
    time.sleep(0.2)
sys.exit("no EXP ResultRecorded within 120s")
EOF

echo "== SIGKILL the run process group =="
RUN_PID="$(cat "$RUN_DIR/run.pid")"
touch "$RUN_DIR/killed"   # releases the fake LLM's deterministic hold on EXP-1
kill -9 -- "-$RUN_PID" 2>/dev/null || kill -9 "$RUN_PID"
set +e
wait "$DRIVE_WRAPPER"
RUN_RC=$?
set -e
echo "run wrapper rc=$RUN_RC (nonzero from SIGKILL is expected)"
rm -f "$RUN_DIR/run.pid"

echo "== continue on the same journal (waits out the ${LEASE_TTL_S}s lease) =="
set +e
uv run coscientist continue "$RUN_DIR/data" \
  --lease-ttl-s "$LEASE_TTL_S" --lease-wait-s 120 \
  "${COMMON_ARGS[@]}" > "$RUN_DIR/continue.log" 2>&1
CONTINUE_RC=$?
set -e
cat "$RUN_DIR/continue.log"
if [ "$CONTINUE_RC" -ne 0 ]; then
  echo "continue failed rc=$CONTINUE_RC"
  exit 1
fi

echo "== journal checker =="
$PY "$HERE/recovery_check.py" "$RUN_DIR/data/$STUDY_ID/events.db" "$STUDY_ID"

echo "RECOVERY OK: study=$STUDY_ID run_dir=$RUN_DIR"
