"""CLI: ``python -m bench run ...``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from bench.adapters.new_runner import NewCoScientistRunner, NewRunnerConfig
from bench.adapters.old_runner import OldCoScientistRunner, OldRunnerConfig
from bench.errors import BenchError
from bench.fakes import FakeRunner, meeting_expectations_factory
from bench.harness import run_suite_and_write
from bench.protocol import BenchRunner


def _load_yaml_mapping(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BenchError(f"{path}: expected a mapping")
    return raw


def _build_fake_pair() -> tuple[BenchRunner, BenchRunner]:
    """Built-in fakes for smoke runs (deterministic provider usage + quality)."""
    old = FakeRunner(
        system_id="old",
        factory=meeting_expectations_factory(
            prompt_tokens=100,
            completion_tokens=50,
            llm_calls=3,
            tool_calls=2,
        ),
    )
    new = FakeRunner(
        system_id="new",
        factory=meeting_expectations_factory(
            prompt_tokens=80,
            completion_tokens=40,
            llm_calls=2,
            tool_calls=2,
        ),
    )
    return old, new


def _build_runners(
    *,
    mode: str,
    runners_config: Path | None,
) -> tuple[BenchRunner, BenchRunner]:
    if mode == "fake":
        return _build_fake_pair()
    if mode != "config":
        raise BenchError(f"unknown --runners mode {mode!r} (expected fake|config)")
    if runners_config is None:
        raise BenchError("--runners config requires --runners-config PATH")
    data = _load_yaml_mapping(runners_config)
    if "old" not in data or "new" not in data:
        raise BenchError(f"{runners_config}: must contain 'old' and 'new' mappings")
    old_raw = data["old"]
    new_raw = data["new"]
    if not isinstance(old_raw, dict) or not isinstance(new_raw, dict):
        raise BenchError(f"{runners_config}: 'old' and 'new' must be mappings")
    old_cfg = OldRunnerConfig.model_validate(old_raw)
    new_cfg = NewRunnerConfig.model_validate(new_raw)
    return OldCoScientistRunner(old_cfg), NewCoScientistRunner(new_cfg)


async def _async_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the task set on old and new runners")
    run_p.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to versioned task-set manifest YAML",
    )
    run_p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for result.json and summary.md",
    )
    run_p.add_argument(
        "--runners",
        choices=("fake", "config"),
        required=True,
        help="fake = in-process fakes; config = load Old/NewRunnerConfig YAML",
    )
    run_p.add_argument(
        "--runners-config",
        type=Path,
        help="YAML with 'old' and 'new' adapter configs (required for --runners config)",
    )

    args = parser.parse_args(argv)
    if args.command != "run":
        raise BenchError(f"unsupported command {args.command!r}")

    old_runner, new_runner = _build_runners(
        mode=args.runners, runners_config=args.runners_config
    )
    suite = await run_suite_and_write(
        manifest_path=args.manifest,
        old_runner=old_runner,
        new_runner=new_runner,
        out_dir=args.out,
    )
    print(
        json.dumps(
            {
                "task_set_id": suite.task_set_id,
                "task_set_version": suite.task_set_version,
                "tasks": len(suite.comparisons),
                "out": str(args.out.resolve()),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(list(sys.argv[1:] if argv is None else argv)))
    except BenchError as exc:
        print(f"bench error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
