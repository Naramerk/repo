"""Structure test for docs/plan/benchmark-harness.md (PR 1 of the bench stack)."""

from __future__ import annotations

from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "docs" / "plan" / "benchmark-harness.md"

REQUIRED_TITLES = (
    "docs(plan): benchmark harness PR list",
    "feat(bench): typed harness errors",
    "feat(bench): task + quality models",
    "feat(bench): provider usage + call/result models",
    "feat(bench): BenchRunner Protocol",
    "feat(bench): versioned easy task set v1",
    "feat(bench): load_tasks from YAML",
    "feat(bench): quality scoring",
    "feat(bench): JSON + markdown report writers",
    "feat(bench): fake runners",
    "feat(bench): suite harness",
    "feat(bench): old-system adapter",
    "feat(bench): new-system adapter",
    "feat(bench): CLI entry",
    "docs(bench): BENCHMARK.md + packaging",
)


def test_benchmark_harness_plan_exists() -> None:
    assert PLAN.is_file(), f"missing plan file: {PLAN}"


def test_benchmark_harness_plan_lists_all_prs() -> None:
    text = PLAN.read_text(encoding="utf-8")
    assert "BenchRunner" in text
    assert "provider usage" in text.lower() or "ProviderTokenUsage" in text
    assert "aimclub/CoScientist_dev" in text
    assert "dev_1" in text
    for title in REQUIRED_TITLES:
        assert title in text, f"plan missing PR title: {title}"
