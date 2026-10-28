#!/usr/bin/env python3
"""Launch wave-10 studies via CLI subprocess with proper argv lists."""
import os
import subprocess
import sys

REPO = "/Users/nargiza/CoScientist-next"

# LAUNCH GUARD (wave-11 lesson): runners import THIS checkout — launching on a
# stale tree silently ships old code. Fast-forward main to origin/main and
# verify the checkout is exactly the fetched tip before any process starts.
def _ensure_fresh_checkout() -> str:
    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(f"launch guard: git {args[0]} failed: {out.stderr[:300]}")
        return out.stdout.strip()
    git("fetch", "origin")
    tip = git("rev-parse", "origin/main")
    head = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main" or head != tip:
        git("checkout", "main")
        git("pull", "--ff-only", "origin", "main")
        head = git("rev-parse", "HEAD")
    if head != tip:
        raise SystemExit(f"launch guard: checkout {head[:9]} != origin/main {tip[:9]}")
    return head



WIRED = '["hypothesizer","researcher","report","critic","reviewer"]'


def base_args(sid: str, task_file: str, *, ds: bool) -> list[str]:
    models = (["--planner-model", "deepseek-v4-flash", "--executor-model",
               "deepseek-v4-flash", "--tz-spec-model", "deepseek-v4-flash"]
              if ds else
              ["--planner-model", "glm-4.7", "--executor-model", "glm-4.7",
               "--tz-spec-model", "glm-4.7"])
    llm = ("bench/lab/glm/llm-deepseek.json" if ds else "bench/lab/glm/llm.json")
    return [
        "uv", "run", "python", "-m", "coscientist.cli", "run",
        "--data-root", "bench/lab/glm/live/bor8",
        "--study-id", sid,
        "--deadline-s", "7200",
        "--lease-ttl-s", "2700",
        "--catalog-version", "5",
        "--catalog", "bench/lab/glm/checks_catalog_v5.json",
        "--profile", "research-deepseek-litsearch" if ds else "research-glm-litsearch",
        "--tool-catalog-version", "2",
        "--owner", "glm-live-run",
        "--llm-config", llm,
        *models,
        "--mcp-tool-bundle", "bench/lab/mcp/fleet_bundle.json",
        "--coder-backend", "remote-openhands",
        "--coder-remote-url", "http://10.32.2.2:8884",
        "--wired-roles", WIRED,
        "--require-hypotheses", "true",
        "--confirmations-needed", "1",
        "--refutations-needed", "1",
        "--refutation-priority", "false",
        "--supported-needed", "2",
        "--max-expansions", "0",
        "--finalize-reserve-s", "60",
        "--rng-seed", "0",
        "--max-llm-calls", "200",
        "--max-tokens", "6000000",
        "--max-tool-calls", "40",
        "--max-ticks", "1200",
        "--hitl-mode", "auto",
        task_file,
    ]


print("launch guard: checkout", _ensure_fresh_checkout()[:9], "= origin/main")
LAUNCHES = [
    ("bor7-ds", sys.argv[1], True),
    ("hyp-tox-ds", sys.argv[2], True),
    ("bor7-glm", sys.argv[3], False),
]
for tag, sid, ds in LAUNCHES:
    args = base_args(sid, "bench/lab/glm/live/borshevik.txt" if tag != "hyp-tox-ds"
                     else "bench/lab/glm/live/mytasks/hyp-cluster-toxicity.txt", ds=ds)
    log = open(f"bench/lab/glm/live/bor8/{sid}.run.log", "w")
    env = dict(os.environ)
    proc = subprocess.Popen(args, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, env=env)
    print(tag, sid, "pid", proc.pid, flush=True)
