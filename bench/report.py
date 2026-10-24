"""Serialize suite results to JSON and a markdown comparison summary."""

from __future__ import annotations

from pathlib import Path

from bench.models import BenchSuiteResult, TimedRunOk, TimedRunRefused, TimedRunResult


def write_json_result(path: Path, suite: BenchSuiteResult) -> None:
    path.write_text(
        suite.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def _fmt_run_metrics(run: TimedRunResult) -> str:
    if isinstance(run, TimedRunRefused):
        return (
            f"refused ({run.reason_code}): {run.detail}; "
            f"wall_time_ms={run.wall_time_ms:.3f}"
        )
    if not isinstance(run, TimedRunOk):
        raise TypeError(f"unexpected run result type: {type(run).__name__}")
    qs = run.quality_score
    return (
        f"wall_time_ms={run.wall_time_ms:.3f}; "
        f"tokens={run.tokens.total_tokens} "
        f"(prompt={run.tokens.prompt_tokens}, completion={run.tokens.completion_tokens}); "
        f"llm_calls={run.calls.llm_calls}; tool_calls={run.calls.tool_calls}; "
        f"report_fields={qs.report_fields_present}/{qs.report_fields_required}; "
        f"plan_criteria={qs.plan_criteria_met}/{qs.plan_criteria_required}; "
        f"verdicts={qs.verdict_count}"
    )


def render_markdown_summary(suite: BenchSuiteResult) -> str:
    lines: list[str] = [
        f"# Benchmark: {suite.task_set_id} (v{suite.task_set_version})",
        "",
        "Tokens are provider usage only (never estimated). Quality tallies are",
        "exact matches against task-config expectations.",
        "",
    ]
    for cmp_ in suite.comparisons:
        lines.append(f"## Task `{cmp_.task_id}`")
        lines.append("")
        lines.append(f"- **old** (`{cmp_.old.system_id}`): {_fmt_run_metrics(cmp_.old)}")
        lines.append(f"- **new** (`{cmp_.new.system_id}`): {_fmt_run_metrics(cmp_.new)}")
        lines.append("")
    return "\n".join(lines)


def write_markdown_summary(path: Path, suite: BenchSuiteResult) -> None:
    path.write_text(render_markdown_summary(suite), encoding="utf-8")
