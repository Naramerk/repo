"""Small study-loop helpers shared by ``run`` / ``continue`` / coder dispatch.

Extracted from ``coscientist.cli.run``. Symbols are re-exported from
``coscientist.cli.run`` for existing importers.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from coscientist.agents.schemas import AgentRun, RegisteredInput, RubricDraft, TaskSpecDraft
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import (
    OutputSpec,
    RequirementRef,
    TaskKind,
    TaskSpec,
)


async def _require_attempt(rt: StudyRuntime, task_key: str) -> str:
    aid = await rt.start_attempt(task_key)
    if aid is None:
        raise CliRefuse(f"start_attempt({task_key!r}) returned None (refused)", code=3)
    return aid


async def _study_refused_detail(rt: StudyRuntime) -> str | None:
    """Last ``StudyRefused.payload`` (kind + reason) from the journal, if any."""
    events = await rt.store.read_all()
    for ev in reversed(events):
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "StudyRefused":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        reason = payload.get("reason")
        if type(kind) is str and type(reason) is str and reason.strip():
            return f"{kind}: {reason.strip()}"
        return "unknown reason"
    return None


async def _registered_user_inputs(rt: StudyRuntime) -> tuple[RegisteredInput, ...]:
    """Journal-sourced registered user inputs (``ArtifactRegistered`` producer=user).

    Typed readback of exactly what the ingest gate journaled — names + CAS
    refs for the planner/executor contexts; never re-derived from CLI paths.
    """
    events = await rt.store.read_all()
    out: list[RegisteredInput] = []
    for ev in events:
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "ArtifactRegistered":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("producer") != "user":
            continue
        name = payload.get("name")
        artifact_id = payload.get("artifact_id")
        uri = payload.get("uri")
        media_type = payload.get("media_type")
        if not (
            type(name) is str
            and name.strip()
            and type(artifact_id) is str
            and artifact_id.strip()
            and type(uri) is str
            and uri.strip()
            and type(media_type) is str
            and media_type.strip()
        ):
            raise CliRefuse(
                f"ArtifactRegistered(user) payload missing typed fields: {payload!r}",
                code=4,
            )
        out.append(
            RegisteredInput(
                name=name,
                artifact_id=artifact_id,
                uri=uri,
                media_type=media_type,
            )
        )
    return tuple(out)


async def _plan_refused_detail(rt: StudyRuntime) -> str:
    """Last ``PlanRefused.payload.detail`` from the journal, if any."""
    events = await rt.store.read_all()
    for ev in reversed(events):
        if not isinstance(ev, StoredEvent):
            continue
        if ev.type != "PlanRefused":
            continue
        payload = ev.payload
        if not isinstance(payload, dict):
            continue
        detail = payload.get("detail")
        if type(detail) is str and detail.strip():
            return detail.strip()
    return ""


def _usage_row(role: str, run: AgentRun, *, task_key: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "role": role,
        "prompt_tokens": run.usage.prompt_tokens,
        "completion_tokens": run.usage.completion_tokens,
        "latency_ms": run.usage.latency_ms,
        "tool_calls": [c.model_dump() for c in run.tool_calls],
    }
    if task_key is not None:
        row["task_key"] = task_key
    return row


def _observed_quality(rt: StudyRuntime) -> dict[str, object]:
    """Observed quality of the study that actually closed (run-summary ``quality``).

    Folded from the same journal state the verdict comes from — never estimated,
    scoped to the closing study's EXPERIMENT tasks (the HER-comparable quantity;
    stage rows like SPEC record results too and are not experiment evidence):

    - ``exp_tasks``: count of EXPERIMENT-kind tasks;
    - ``evidence``: outcome tallies over their recorded results, sorted by
      outcome name (deterministic; retries each count, as in the HER extract);
    - ``record_result``: count of those results.

    The runtime fold keys every result row twice (by ``result_id`` and by
    ``attempt_id``), so rows are deduplicated by payload ``result_id``
    (schema-required) before tallying; the result's task kind goes through the
    folded attempt row (``attempts[attempt_id].task_id``), never id parsing.
    Additive summary keys; consumers (bench harness, API lane, humans) read one
    declared output.
    """
    rows: dict[str, dict[str, Any]] = {}
    for result in rt.results.values():
        rows[str(result["result_id"])] = result
    exp_rows: list[dict[str, Any]] = []
    for row in rows.values():
        attempt = rt.attempts[str(row["attempt_id"])]
        task = rt.tasks[str(attempt["task_id"])]
        if task.kind == TaskKind.EXPERIMENT:
            exp_rows.append(row)
    tally: Counter[str] = Counter(str(row["outcome"]) for row in exp_rows)
    return {
        "exp_tasks": sum(1 for task in rt.tasks.values() if task.kind == TaskKind.EXPERIMENT),
        "evidence": [{"outcome": outcome, "count": tally[outcome]} for outcome in sorted(tally)],
        "record_result": len(exp_rows),
    }


def _rubric_from_draft(draft: RubricDraft) -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=it.id,
                    text=it.text,
                    required=it.required,
                    check=it.check,
                    code_kind=it.code_kind,
                )
                for it in draft.items
            ],
            aggregate=draft.aggregate,
            k=draft.k,
        )
    )


def _to_task_spec(draft: TaskSpecDraft) -> TaskSpec:
    """Map planner draft → core ``TaskSpec``. Rubrics come only from the draft."""
    outputs: list[OutputSpec] = []
    for o in draft.expected_outputs:
        if o.rubric is None:
            item_rubric = None
        else:
            item_rubric = _rubric_from_draft(o.rubric)
        outputs.append(
            OutputSpec(
                name=o.name,
                media_type=o.media_type,
                schema_ref=o.schema_ref,
                required=o.required,
                rubric=item_rubric,
            )
        )
    return TaskSpec(
        task_key=draft.task_key,
        title=draft.title,
        goal=draft.goal,
        route=draft.route,
        depends_on=list(draft.depends_on),
        addresses=list(draft.addresses),
        tools_hint=list(draft.tools_hint),
        expected_outputs=outputs,
        criteria=[RequirementRef(template_id=c) for c in draft.criteria],
    )
