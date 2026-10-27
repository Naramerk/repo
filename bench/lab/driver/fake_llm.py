#!/usr/bin/env python3
"""Fake OpenAI-compatible LLM + fixtures + killable spawn for the recovery lane.

Stateless across restarts: responses are keyed by REQUEST CONTENT, not a
cursor — a killed run and its ``continue`` successor see consistent bodies.

- request body contains ``"EXP-0"`` → executor body for EXP-0 (dataset_overview)
- request body contains ``"EXP-1"`` → executor body for EXP-1 (clustering);
  held for ``--exp1-hold-s`` while no ``killed`` marker exists, so the driver's
  SIGKILL lands deterministically mid-EXP-1 (the run is blocked in the LLM
  call with an open attempt, exactly "killed after first record_result").
- otherwise: first non-executor request → ResearchSpec, second → PlanIntent.

Subcommands:
  fixtures  write task.txt / tools.json / llm.json / bodies/*.json into --dir
  serve     run the HTTP server (GET /healthz for readiness)
  spawn     start a command in its own session (process group), write its PID
            to --pidfile, wait, propagate the exit code
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _completion(content: str, *, model: str = "fake-llm") -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def _bodies() -> dict[str, str]:
    """Cassette contents (same typed shapes as tests/cli/test_run_study.py)."""
    from coscientist.agents.research_spec import (
        Constraint,
        Goal,
        ResearchSpec,
        SuccessMetric,
    )
    from coscientist.agents.schemas import (
        ExecutorRouteOut,
        PlanIntent,
        QuestionIntent,
        TaskIntent,
        ToolArg,
    )
    from coscientist.core.execution.domain import Need

    spec = ResearchSpec(
        object="two_tasks recovery study",
        goal=Goal(
            statement="complete two experiment tasks sequentially",
            research_form=None,
            trl=None,
        ),
        scope="synthetic",
        constraints=(Constraint(subtype="resource", content="cassette LLM", limit=None),),
        success_metrics=(
            SuccessMetric(metric="tasks", op="==", target="2", check="code", tolerance=None),
        ),
    ).model_dump_json()

    def _exec(tool: str) -> str:
        return ExecutorRouteOut(
            summary=f"ran {tool}",
            values=(
                ToolArg(key="ok", value_json="true"),
                ToolArg(key="main", value_json='{"ok":true}'),
            ),
            tool_found=(tool,),
            # Required-nullable (strict structured outputs): explicit None.
            expansion_request=None,
        ).model_dump_json()

    plan = PlanIntent(
        tasks=(
            TaskIntent(
                goal="overview",
                route="experiment",
                key="EXP-0",
                    tools_hint=("static:dataset_overview",),
                    question_indices=(1,),
            ),
            TaskIntent(
                goal="cluster",
                route="experiment",
                key="EXP-1",
                depends_on=("EXP-0",),
                    tools_hint=("static:chemical_space_clustering",),
                    question_indices=(1,),
            ),
        ),
            questions=(QuestionIntent(text="two tasks?", final_type="artifact"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    ).model_dump_json()

    return {
        "spec": spec,
        "plan": plan,
        "exec0": _exec("dataset_overview"),
        "exec1": _exec("chemical_space_clustering"),
    }


def cmd_fixtures(args: argparse.Namespace) -> int:
    out = Path(args.dir)
    (out / "bodies").mkdir(parents=True, exist_ok=True)
    bodies = _bodies()
    for name, content in bodies.items():
        (out / "bodies" / f"{name}.json").write_text(
            json.dumps(_completion(content)), encoding="utf-8"
        )
    (out / "task.txt").write_text(
        "Build dataset overview and chemical space clustering (two tasks).\n",
        encoding="utf-8",
    )
    (out / "tools.json").write_text(
        json.dumps(
            {
                "dataset_overview": {"result": {"rows": 10}, "args": {}},
                "chemical_space_clustering": {"result": {"clusters": 3}, "args": {}},
            }
        ),
        encoding="utf-8",
    )
    (out / "llm.json").write_text(
        json.dumps(
            {
                "base_url": f"http://{args.host}:{args.port}",
                "api_key_env": "COSCI_TEST_LLM_KEY",
                "chat_completions_path": "chat/completions",
                "max_tokens_field": "max_tokens",
                "timeout_s": 20.0,
                "total_deadline_s": 300.0,
                "max_retries": 0,
                "retry_backoff_s": 0.0,
                "retry_backoff_multiplier": 1.0,
                "retry_backoff_jitter_s": 0.0,
                "retry_backoff_max_s": 1.0,
                "retry_on_timeout": False,
                "honor_retry_after": False,
                "retryable_status_codes": [],
                "max_completion_tokens": 4096,
                "accepted_finish_reasons": ["stop"],
                "unexpected_transport_retryable": False,
            }
        ),
        encoding="utf-8",
    )
    print(f"fixtures written to {out}")
    return 0


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-llm/1"

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        cfg = self.server.cfg  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            if Path(self.server.cfg["requests_log"]).parent.exists():  # type: ignore[attr-defined]
                with open(self.server.cfg["requests_log"], "ab") as fh:  # type: ignore[attr-defined]
                    fh.write(raw + b"\n---\n")
            # The user message embeds the payload JSON with escaped quotes, so
            # the executor task key appears as \"task_key\":\"EXP-n\" — match
            # that exact form (a bare "EXP-0" substring would also hit EXP-1's
            # request via its depends_on list).
            if b'\\"task_key\\":\\"EXP-0\\"' in raw:
                body = cfg["bodies"]["exec0"]
            elif b'\\"task_key\\":\\"EXP-1\\"' in raw:
                if cfg["exp1_hold_s"] > 0 and not Path(cfg["killed"]).exists():
                    # Deterministic kill window: block the first EXP-1 executor
                    # call until the driver marks the kill (or the hold ends).
                    time.sleep(cfg["exp1_hold_s"])
                body = cfg["bodies"]["exec1"]
            else:
                with cfg["nz_lock"]:
                    nz = cfg["non_executor_seen"]
                    cfg["non_executor_seen"] = nz + 1
                body = cfg["bodies"]["spec"] if nz == 0 else cfg["bodies"]["plan"]
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:  # noqa: BLE001 — dead-client socket after SIGKILL is expected
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


def cmd_serve(args: argparse.Namespace) -> int:
    bodies_dir = Path(args.dir) / "bodies"
    cfg: dict[str, Any] = {
        "bodies": {
            name: (bodies_dir / f"{name}.json").read_text(encoding="utf-8")
            for name in ("spec", "plan", "exec0", "exec1")
        },
        "killed": str(Path(args.dir) / "killed"),
        "exp1_hold_s": float(args.exp1_hold_s),
        "non_executor_seen": 0,
        "nz_lock": threading.Lock(),
        "requests_log": str(Path(args.dir) / "requests.log"),
    }
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    print(f"fake-llm serving on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()
    return 0


def cmd_spawn(args: argparse.Namespace) -> int:
    command = [c for c in (args.command or []) if c != "--"]
    if not command:
        raise SystemExit("spawn: empty command")
    proc = subprocess.Popen(
        command,
        start_new_session=True,  # own process group → driver can SIGKILL the group
    )
    Path(args.pidfile).write_text(str(proc.pid), encoding="utf-8")
    print(f"spawned pid={proc.pid} pgid={proc.pid}: {command}", flush=True)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_llm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fixtures = sub.add_parser("fixtures")
    fixtures.add_argument("--dir", required=True)
    fixtures.add_argument("--host", default="127.0.0.1")
    fixtures.add_argument("--port", type=int, required=True)
    fixtures.set_defaults(fn=cmd_fixtures)

    serve = sub.add_parser("serve")
    serve.add_argument("--dir", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--exp1-hold-s", type=float, default=600.0)
    serve.set_defaults(fn=cmd_serve)

    spawn = sub.add_parser("spawn")
    spawn.add_argument("--pidfile", required=True)
    spawn.add_argument("command", nargs=argparse.REMAINDER)
    spawn.set_defaults(fn=cmd_spawn)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
