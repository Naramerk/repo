"""Post-finalize report lane: scientific report + ГОСТ ТЗ/НИР into the CAS.

Runs AFTER the tick loop closes (the research graph is complete only
post-finalize; journal writes are closed by I3, so documents land as CAS
artifacts — the same store finalize's ``report.md`` uses). Verdict semantics
are untouched: this lane reads the projections and never writes journal
events, so report content cannot flip a verdict.

LLM discipline mirrors ``LlmAgentAdapter._complete_validated``: the JSON
schema is echoed in the system message; one bounded repair round per document
(schema error OR honesty violation is fed back once), then a typed refuse
(``CliRefuse`` code 8). Budget: the lane checks ``Budget.deadline`` before
every call and is structurally bounded to two calls per document (≤ 6 total);
adapter-side counters are private, so token totals are recorded in the usage
row rather than re-enforced here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.core.events.clock import Clock
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import Artifact, Budget, ToolCallRef
from coscientist.llm.client import LlmClient
from coscientist.llm.types import ChatMessage, JsonSchemaResponseFormat, StructuredLlmRequest
from coscientist.reporting.gost_tz import TzProse, build_tz_markdown
from coscientist.reporting.graph_digest import DigestRefuse, GraphDigest, build_graph_digest
from coscientist.reporting.honesty import DigestFacts, validate_prose
from coscientist.reporting.nir import NirOutline, NirProse, build_nir_markdown, build_nir_outline
from coscientist.reporting.prompts import NIR_SYSTEM, SCIENCE_REPORT_SYSTEM, TZ_SYSTEM
from coscientist.reporting.science import ScienceReportProse, build_science_report_markdown

SCIENCE_ARTIFACT_NAME = "report.science.md"
TZ_ARTIFACT_NAME = "tz.gost-19.201-78.md"
NIR_ARTIFACT_NAME = "nir.gost-7.32-2017.md"

# Structural bound: one initial call + one repair round per document.
_MAX_CALLS_PER_DOC = 2

_T = TypeVar("_T", bound=BaseModel)


@runtime_checkable
class _ArtifactStoreProtocol(Protocol):
    def put(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        role: str | None = None,
    ) -> Artifact: ...


def human_gate_feedback(events: Sequence[StoredEvent]) -> list[dict[str, str]]:
    """Fold journal events into human gate-feedback rows (HITL v2, hitl-map §5).

    A ``HitlDecided`` with non-empty ``feedback`` becomes one row; the gate
    kind comes from the paired ``HitlOpened`` (request_id join). Rows are
    journal facts — verbatim human text, never model prose.
    """
    kind_by_rid: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for event in events:
        payload = event.payload
        if not isinstance(payload, Mapping):
            continue
        rid = payload.get("request_id")
        if type(rid) is not str:
            continue
        event_type = event.type
        if event_type == "HitlOpened":
            kind = payload.get("kind")
            if type(kind) is str and kind:
                kind_by_rid[rid] = kind
        elif event_type == "HitlDecided":
            feedback = payload.get("feedback")
            decision = payload.get("decision")
            if type(feedback) is not str or not feedback:
                continue
            if type(decision) is not str or not decision:
                continue
            gate = kind_by_rid.get(rid)
            if type(gate) is not str or not gate:
                gate = rid
            rows.append({"gate": gate, "decision": decision, "feedback": feedback})
    return rows


def _json_schema_response_format(model: type[BaseModel], *, name: str) -> JsonSchemaResponseFormat:
    return JsonSchemaResponseFormat(name=name, strict=True, schema=model.model_json_schema())


def _prose_texts(model: BaseModel) -> tuple[str, ...]:
    """Every model-written string the honesty gate must check."""
    if isinstance(model, ScienceReportProse):
        return tuple(text for _, text in model.slots())
    if isinstance(model, TzProse):
        return (*model.prose_texts(), model.topic)
    if isinstance(model, NirProse):
        return model.prose_texts()
    raise CliRefuse(f"report lane: unknown prose model {type(model).__name__}", code=8)


def _deadline_allows_call(budget: Budget, clock: Clock) -> bool:
    deadline = budget.deadline
    if deadline is None:
        return True
    if deadline.tzinfo is None or clock.now() >= deadline:
        return False
    return True


async def _complete_prose(
    client: LlmClient,
    *,
    model_id: str,
    system: str,
    user: str,
    out_cls: type[_T],
    schema_name: str,
    facts: DigestFacts,
    budget: Budget,
    clock: Clock,
) -> tuple[_T, dict[str, int]]:
    """Complete → schema-validate → honesty-validate; one repair round total."""
    response_format = _json_schema_response_format(out_cls, name=schema_name)
    system_full = (
        system
        + "\nExact JSON Schema of the required response:\n"
        + json.dumps(response_format.schema_, ensure_ascii=False, separators=(",", ":"))
    )
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_full),
        ChatMessage(role="user", content=user),
    ]
    problem = ""
    calls = 0
    last_content = ""
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    for attempt in (0, 1):
        if attempt and (calls >= _MAX_CALLS_PER_DOC or not _deadline_allows_call(budget, clock)):
            break
        if not _deadline_allows_call(budget, clock):
            raise CliRefuse(
                f"report lane refused ({schema_name}): budget deadline exceeded", code=8
            )
        if attempt:
            messages = [
                *messages,
                ChatMessage(role="assistant", content=last_content),
                ChatMessage(
                    role="user",
                    content=(
                        "Предыдущий ответ отклонён:\n"
                        + problem
                        + "\nОтветь снова полным исправленным JSON; новые факты и числа "
                        "не добавляй."
                    ),
                ),
            ]
        response = await client.complete(
            StructuredLlmRequest(
                model=model_id, messages=messages, response_format=response_format
            )
        )
        calls += 1
        usage_total["prompt_tokens"] += response.usage.prompt_tokens
        usage_total["completion_tokens"] += response.usage.completion_tokens
        last_content = response.content
        try:
            parsed = out_cls.model_validate_json(response.content)
        except ValidationError as exc:
            problem = f"схема JSON не соблюдена: {exc}"
        else:
            violations: list[str] = []
            for text in _prose_texts(parsed):
                violations.extend(validate_prose(text, facts))
            if not violations:
                return parsed, usage_total
            problem = "\n".join(violations)
    raise CliRefuse(
        f"report lane refused ({schema_name}): honesty/schema validation failed "
        f"after one repair round: {problem}",
        code=8,
    )


def _artifact_store(rt: StudyRuntime) -> _ArtifactStoreProtocol:
    executor = rt.executor
    arts = executor.artifacts if executor is not None else None
    if arts is None:
        arts = rt.artifacts
    if not isinstance(arts, _ArtifactStoreProtocol):
        raise CliRefuse("report lane: no artifact store on runtime", code=8)
    return arts


def _put_markdown(
    arts: _ArtifactStoreProtocol,
    *,
    study_id: str,
    name: str,
    markdown: str,
    role: str,
) -> str:
    artifact = arts.put(
        markdown.encode("utf-8"),
        make_tool_ref(f"report-lane:{study_id}"),
        name=name,
        media_type="text/markdown",
        role=role,
    )
    return str(artifact.artifact_id)


def _tz_user(digest: GraphDigest) -> str:
    return (
        "ДАЙДЖЕСТ:\n"
        + digest.markdown
        + '\nЗаполни разделы 1, 2, 4, 6 (поле sections; number — строки "1"/"2"/"4"/"6").'
    )


def _nir_user(digest: GraphDigest, outline: NirOutline) -> str:
    section_rows = "\n".join(
        f"- {section.id}: {section.title_hint} — факты: " + " | ".join(section.digest_facts)
        for section in outline.sections
    )
    return (
        "ДАЙДЖЕСТ:\n"
        + digest.markdown
        + "\nИдентификаторы разделов для section_texts/section_titles:\n"
        + section_rows
        + f"\nИсследовательский вопрос: {outline.question}"
    )


async def render_report_docs(
    rt: StudyRuntime,
    *,
    client: LlmClient,
    model_id: str,
    budget: Budget,
    clock: Clock,
    usages: list[dict[str, object]],
    human_feedback: Sequence[Mapping[str, str]] = (),
    science_note: str | None = None,
) -> tuple[dict[str, str], GraphDigest, ScienceReportProse, str]:
    """Render the three documents from the graph projection; CAS them.

    ``human_feedback`` rows fold into the science report's «Замечания человека»
    section (journal facts, never model prose). ``science_note`` (HITL v2
    report-gate repair) carries the human's exact feedback into the science
    user payload for the ONE bounded re-render.
    """
    study = rt.study
    if study is None or not study.study_id:
        raise CliRefuse("report lane: runtime has no study_id", code=8)
    study_id = study.study_id
    try:
        nodes = await rt.store.get_projection("graph_nodes")
        edges = await rt.store.get_projection("graph_edges")
        digest = build_graph_digest(study_id, nodes, edges)
    except DigestRefuse as exc:
        raise CliRefuse(f"report lane: digest refused: {exc.detail}", code=8) from exc
    facts = digest.facts()
    arts = _artifact_store(rt)

    science_user = "ДАЙДЖЕСТ:\n" + digest.markdown
    if science_note is not None:
        science_user += (
            "\nЧеловек вернул отчёт с замечаниями: "
            + science_note
            + ". Перепиши отчёт, исправив их, на том же дайджесте."
        )
    science, science_usage = await _complete_prose(
        client,
        model_id=model_id,
        system=SCIENCE_REPORT_SYSTEM,
        user=science_user,
        out_cls=ScienceReportProse,
        schema_name="science_report_prose",
        facts=facts,
        budget=budget,
        clock=clock,
    )
    tz, tz_usage = await _complete_prose(
        client,
        model_id=model_id,
        system=TZ_SYSTEM,
        user=_tz_user(digest),
        out_cls=TzProse,
        schema_name="tz_prose",
        facts=facts,
        budget=budget,
        clock=clock,
    )
    outline = build_nir_outline(digest)
    nir, nir_usage = await _complete_prose(
        client,
        model_id=model_id,
        system=NIR_SYSTEM,
        user=_nir_user(digest, outline),
        out_cls=NirProse,
        schema_name="nir_prose",
        facts=facts,
        budget=budget,
        clock=clock,
    )

    science_markdown = build_science_report_markdown(digest, science, human_feedback=human_feedback)
    artifacts = {
        SCIENCE_ARTIFACT_NAME: _put_markdown(
            arts,
            study_id=study_id,
            name=SCIENCE_ARTIFACT_NAME,
            markdown=science_markdown,
            role="report",
        ),
        TZ_ARTIFACT_NAME: _put_markdown(
            arts,
            study_id=study_id,
            name=TZ_ARTIFACT_NAME,
            markdown=build_tz_markdown(digest, tz),
            role="report",
        ),
        NIR_ARTIFACT_NAME: _put_markdown(
            arts,
            study_id=study_id,
            name=NIR_ARTIFACT_NAME,
            markdown=build_nir_markdown(digest, outline, nir),
            role="report",
        ),
    }
    usages.append(
        {
            "role": "report",
            "model": model_id,
            "documents": sorted(artifacts),
            "artifact_ids": artifacts,
            "prompt_tokens": (
                science_usage["prompt_tokens"] + tz_usage["prompt_tokens"] + nir_usage["prompt_tokens"]
            ),
            "completion_tokens": (
                science_usage["completion_tokens"]
                + tz_usage["completion_tokens"]
                + nir_usage["completion_tokens"]
            ),
        }
    )
    return artifacts, digest, science, science_markdown


def put_science_report(rt: StudyRuntime, *, study_id: str, markdown: str) -> str:
    """One CAS put of the final science report markdown (report-gate close)."""
    return _put_markdown(
        _artifact_store(rt),
        study_id=study_id,
        name=SCIENCE_ARTIFACT_NAME,
        markdown=markdown,
        role="report",
    )


async def run_report_lane(
    rt: StudyRuntime,
    *,
    client: LlmClient,
    model_id: str,
    budget: Budget,
    clock: Clock,
    usages: list[dict[str, object]],
) -> dict[str, str]:
    """Generate the three documents from the graph projection; CAS them."""
    artifacts, _digest, _prose, _markdown = await render_report_docs(
        rt, client=client, model_id=model_id, budget=budget, clock=clock, usages=usages,
    )
    return artifacts


__all__ = [
    "NIR_ARTIFACT_NAME",
    "SCIENCE_ARTIFACT_NAME",
    "TZ_ARTIFACT_NAME",
    "human_gate_feedback",
    "put_science_report",
    "render_report_docs",
    "run_report_lane",
]
