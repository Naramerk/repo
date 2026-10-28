#!/bin/bash
# Recover bor7 attempt-3 (crashed on lease fencing) via CLI continue + relaunch lit-vs-pred.
set -uo pipefail
REPO=/Users/nargiza/CoScientist-next
cd "$REPO"

# continue bor7-3 from its journal (accepted plan survives; executor resumes)
nohup uv run python -m coscientist.cli continue \
  "$REPO/bench/lab/glm/live/bor8" \
  --study-id st_idem_a341e11b644777a4947211a4 \
  --catalog "$REPO/bench/lab/glm/checks_catalog.json" \
  --catalog-version 5 \
  --profile research-glm-litsearch --tool-catalog-version 2 \
  --owner glm-live-run \
  --llm-config "$REPO/bench/lab/glm/llm.json" \
  --planner-model glm-4.7 --executor-model glm-4.7 --tz-spec-model glm-4.7 \
  --mcp-tool-bundle "$REPO/bench/lab/mcp/fleet_bundle.json" \
  --lease-ttl-s 600 --lease-wait-s 660 --rng-seed 0 \
  --max-llm-calls 200 --max-tokens 6000000 --max-tool-calls 40 --max-ticks 1200 \
  > "$REPO/bench/lab/glm/live/bor8/st_idem_a341e11b644777a4947211a4.continue.log" 2>&1 &
echo "bor7-3 continue launched"

# relaunch lit-vs-pred (its plan gate expired)
python3 - << 'EOF'
import json, urllib.request
text = open("/Users/nargiza/CoScientist-next/bench/lab/glm/live/mytasks/lit-vs-prediction.txt").read()
body = {"preset_id": "autonomous-full-fleet", "task_text": text,
        "idempotency_key": "mytask-lit-vs-pred-20261028-c"}
req = urllib.request.Request("http://127.0.0.1:8487/console/runs",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=150) as r:
    print("lit-vs-pred relaunched:", json.load(r)["study_id"], flush=True)
EOF
echo done
