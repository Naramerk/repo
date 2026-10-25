"""CLI entry (plan item 14): ``python -m bench run --suite DIR --runner NAME --out DIR``.

Loads the versioned task set, builds the registered runner, runs the suite,
and writes ``result.json`` / ``summary.md`` / ``usage.jsonl`` into ``--out``.
The runner name doubles as ``system_id`` in the artifacts (which system produced
the runs). Every wiring problem is a typed ``BenchError`` printed to stderr with
exit code 2 — nothing is silently skipped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from bench.errors import BenchError
from bench.load_tasks import load_manifest, load_tasks
from bench.runners import build_runner
from bench.suite import run_suite, write_suite_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench", description="CoScientist benchmark harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run a task set through a registered runner")
    run_p.add_argument("--suite", type=Path, required=True, help="task set directory (manifest.yaml)")
    run_p.add_argument("--runner", required=True, help="registered runner name (system_id)")
    run_p.add_argument("--out", type=Path, required=True, help="output directory for artifacts")
    return parser


def _run_command(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.suite)
    manifest = load_manifest(args.suite)
    runner = build_runner(args.runner)
    result = run_suite(
        runner,
        tasks,
        system_id=args.runner,
        task_set_id=manifest.task_set_id,
        task_set_version=manifest.task_set_version,
    )
    write_suite_artifacts(args.out, result)
    print(f"bench: wrote {args.out / 'result.json'}, {args.out / 'summary.md'}, {args.out / 'usage.jsonl'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run_command(args)
    except BenchError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command!r}")
    return 2
