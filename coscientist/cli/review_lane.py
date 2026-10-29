"""Post-finalize review lane: reviewer role over the graph digest → ``review.md``.

Same lane pattern as ``report_lane``: runs AFTER the tick loop closes (the
research graph is complete only post-finalize), reads the projections, and
writes NO journal events — the recorded verdict can never flip. The reviewer
runs through ``LlmAgentAdapter`` (the wired ``reviewer`` role), so the schema
echo, the one bounded repair round, and ``Budget`` enforcement are the
adapter's (``_complete_validated``) — not a second implementation here.

The rendered document is advisory commentary: deterministic markdown that
places the typed assessment/gaps beside the digest they describe (the digest
is embedded verbatim, so every claim is checkable against it). Unlike the
report documents it is not prose-gated by the honesty validator — it never
enters the journal and gates nothing.
"""

from __future__ import annotations

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.schemas import ReviewerIn, ReviewerOut, StudyContext
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.cli.report_lane import (
    _artifact_store,
    _put_markdown,
)
from coscientist.core.execution.domain import Budget
from coscientist.reporting.graph_digest import DigestRefuse, GraphDigest, build_graph_digest

REVIEW_ARTIFACT_NAME = "review.md"


def _render_review(review: ReviewerOut, digest: GraphDigest) -> str:
    """Deterministic markdown: assessment + gaps beside the digest facts."""
    gaps = "\n".join(f"- {gap}" for gap in review.gaps) if review.gaps else "- (не зафиксировано)"
    return (
        "# Ревью результатов исследования\n\n"
        "## Оценка (assessment)\n\n"
        f"{review.assessment}\n\n"
        "## Пробелы (gaps)\n\n"
        f"{gaps}\n\n"
        "## Дайджест графа (источник фактов)\n\n"
        f"{digest.markdown}"
    )


async def run_review_lane(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    ctx: StudyContext,
    budget: Budget,
    usages: list[dict[str, object]],
) -> str:
    """Review the closed graph digest → CAS ``review.md``; return its artifact id."""
    study = rt.study
    if study is None or not study.study_id:
        raise CliRefuse("review lane: runtime has no study_id", code=8)
    study_id = study.study_id
    try:
        nodes = await rt.store.get_projection("graph_nodes")
        edges = await rt.store.get_projection("graph_edges")
        digest = build_graph_digest(study_id, nodes, edges)
    except DigestRefuse as exc:
        raise CliRefuse(f"review lane: digest refused: {exc.detail}", code=8) from exc
    review_run = await adapter.run(
        "reviewer",
        ReviewerIn(context=ctx, digest_markdown=digest.markdown),
        [],
        budget,
    )
    if review_run.error is not None:
        raise CliRefuse(f"reviewer error: {review_run.error}", code=4)
    review = review_run.output
    if not isinstance(review, ReviewerOut):
        raise CliRefuse("reviewer must return ReviewerOut", code=4)
    artifact_id = _put_markdown(
        _artifact_store(rt),
        study_id=study_id,
        name=REVIEW_ARTIFACT_NAME,
        markdown=_render_review(review, digest),
        role="reviewer",
    )
    # Lane usage row (same shape as the report lane): honest token/cost record
    # in the run summary; no journal events.
    usages.append(
        {
            "role": "reviewer",
            "artifact_ids": {REVIEW_ARTIFACT_NAME: artifact_id},
            "assessment": review.assessment,
            "gaps": list(review.gaps),
            "prompt_tokens": review_run.usage.prompt_tokens,
            "completion_tokens": review_run.usage.completion_tokens,
            "latency_ms": review_run.usage.latency_ms,
            "tool_calls": [c.model_dump() for c in review_run.tool_calls],
        }
    )
    return artifact_id


__all__ = ["REVIEW_ARTIFACT_NAME", "run_review_lane"]
