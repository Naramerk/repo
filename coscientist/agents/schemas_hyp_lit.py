"""Wired HYP / LIT role schemas (DESIGN §7.1 hypothesizer / researcher).

Extracted from ``schemas`` so planner/executor types can evolve without
touching this additive block. Consumers import the names from this module
directly — a module-level re-export on ``schemas`` would cycle (this module
imports ``AgentModel`` from ``schemas``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pydantic import Field, ValidationError, model_validator

from coscientist.agents.schemas import AgentModel

# --- Wired optional roles (DESIGN §7.1 hypothesizer / researcher) -------------
# Additive block: new typed outputs for the HYP / LIT need stages (was kept at
# the end of schemas.py so concurrent schema edits elsewhere did not conflict).


class HypothesisDraft(AgentModel):
    """One hypothesis row, v2 (DESIGN §2: id/statement/rank/prediction/selected).

    v2 adds the legacy-H1..H4 shape: a checkable numeric/structural
    ``prediction``, a competing-set ``rank``, the research ``question_ref``
    this hypothesis addresses, and the exactly-one ``selected`` flag the
    RC5/RC6 gates already require. Writing stays fail-closed v2; reading old
    v1 CAS rows goes through :func:`hypothesis_rows_v1` (display only).
    """

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prediction: str = Field(min_length=1)
    rank: int
    question_ref: str = Field(min_length=1)
    selected: bool


class HypothesisRowsV1(AgentModel):
    """Legacy v1 hypothesis row (id/statement/rationale only) — read-compat.

    Never a plan input: RC5/RC6 gates keep refusing v1 rows (missing
    prediction/rank/selected). For display / diagnostics of old CAS artifacts.
    """

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


def hypothesis_rows_v1(raw: object) -> tuple[HypothesisRowsV1, ...] | None:
    """Recognize legacy v1 hypothesis rows in a HYP artifact ``values`` blob.

    Returns v1 rows only when the shape is exactly legacy v1 (rows carry
    id/statement/rationale and none carries a v2 field). Anything else —
    including v2 payloads and malformed blobs — returns ``None`` (callers
    fall back to strict v2 parsing, which stays fail-closed).
    """
    if not isinstance(raw, dict):
        return None
    rows = raw.get("hypotheses")
    if not isinstance(rows, list) or not rows:
        return None
    v2_fields = ("prediction", "rank", "question_ref", "selected")
    out: list[HypothesisRowsV1] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if any(f in row for f in v2_fields):
            return None
        try:
            out.append(HypothesisRowsV1.model_validate(row))
        except ValidationError:
            return None  # audit-ok[except_silent_return]: not-a-v1-shape → caller falls back to strict v2 parsing
    return tuple(out)


class HypothesisOut(AgentModel):
    """hypothesizer structured output for the wired HYP stage (DESIGN §7.1, v2).

    Floor of three and exactly one ``selected`` mirror the RC5 artifact gate;
    unique ranks keep the competing set a total order for foreach selection.
    """

    hypotheses: tuple[HypothesisDraft, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _unique_ids(self) -> HypothesisOut:
        ids = [h.id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("hypotheses ids must be unique (no duplicate references)")
        return self

    @model_validator(mode="after")
    def _unique_ranks(self) -> HypothesisOut:
        ranks = [h.rank for h in self.hypotheses]
        if len(ranks) != len(set(ranks)):
            raise ValueError("hypotheses ranks must be unique (total competing-set order)")
        return self

    @model_validator(mode="after")
    def _exactly_one_selected(self) -> HypothesisOut:
        selected = [h for h in self.hypotheses if h.selected]
        if len(selected) != 1:
            raise ValueError(
                f"exactly one hypothesis must be selected=True, got {len(selected)}"
            )
        return self


class PaperRef(AgentModel):
    """One paper ref from LIT tool outputs (evidence-style, tool-backed).

    ``identity_key`` is the explicit paper identity key (spec-lit SL6/SL7: no
    DOI/URL normalization heuristics). ``source_tool`` must be the exact
    journaled ``server:tool`` tool_call that surfaced this paper — the record
    path refuses papers that do not trace to a tool result (no invented
    citations).
    """

    title: str = Field(min_length=1)
    identity_key: str = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    doi: str | None = None  # audit-ok[field_default]: optional metadata; identity gate is identity_key, never doi/url
    url: str | None = None  # audit-ok[field_default]: optional metadata; identity gate is identity_key, never doi/url


class LiteratureOut(AgentModel):
    """researcher structured output for the wired LIT stage (DESIGN §7.1)."""

    papers: tuple[PaperRef, ...]


# Receipt identity fields the live literature tools actually surface
# (papers-search:search_papers → article_id; paper-analysis → doi; the
# spec-lit canonical name). Only a fact-extraction aid — the binding gate
# itself stays substring containment, never a second path.
_IDENTITY_FIELDS = ("identity_key", "article_id", "doi")
_RECEIPT_FACTS_LIMIT = 20


def _identity_field_values(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _IDENTITY_FIELDS and isinstance(value, str) and value:
                found.append(value)
            else:
                _identity_field_values(value, found)
    elif isinstance(node, list):
        for item in node:
            _identity_field_values(item, found)
    elif isinstance(node, str):
        # MCP tool envelopes carry the payload as a JSON-encoded string
        # ({"result":{"content":[{"type":"text","text":"<json>"}]}}) — unwrap
        # any string that parses so nested identity fields surface.
        try:
            parsed = json.loads(node)
        except ValueError:
            return  # audit-ok[except_silent_return]: not JSON-encoded — an ordinary leaf, nothing to unwrap
        _identity_field_values(parsed, found)


def receipt_identity_keys(tool_results: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Identity facts per tool: string values of ``_IDENTITY_FIELDS`` parsed
    from the journaled ok result bodies. Best effort — non-JSON bodies
    contribute nothing (the gate is substring containment, unchanged)."""
    facts: dict[str, list[str]] = {}
    for tool_name, bodies in tool_results.items():
        values: list[str] = []
        for body in bodies:
            # Best-effort facts: a non-JSON receipt body contributes nothing —
            # the binding gate is substring containment, never parse-dependent.
            try:
                parsed = json.loads(body)
            except ValueError:
                continue  # audit-ok[except_silent_continue]: best-effort facts; gate is substring containment
            _identity_field_values(parsed, values)
        facts[tool_name] = values[:_RECEIPT_FACTS_LIMIT]
    return facts


def literature_binding_error(
    out: LiteratureOut, *, tool_results: Mapping[str, Sequence[str]]
) -> str | None:
    """Refuse detail for papers that do not trace to journaled tool results,
    or None when every paper binds. One gate, two consumers: the automatic
    loop feeds it back to the SAME agent as a repair message (owner law, bor7
    st_1a17e98a04 LIT#1: a final-output validation error must not fall the
    run), and ``journal.record_literature`` keeps it as the final fail-closed
    refuse. The detail carries receipt facts (identity fields the cited tools
    actually surfaced + the cited keys) so the repair round can fix the
    output targeted."""
    problems: list[str] = []
    for i, paper in enumerate(out.papers):
        bodies = tool_results.get(paper.source_tool)
        if not bodies:
            problems.append(
                f"paper[{i}] cites {paper.source_tool!r} but no journaled tool result "
                f"carries that name (tools={sorted(tool_results)!r}) — no invented citations"
            )
            continue
        if not any(paper.identity_key in body for body in bodies):
            problems.append(
                f"paper[{i}] identity_key not found in the {paper.source_tool!r} result — "
                "papers only from tool outputs (no invented citations)"
            )
    if not problems:
        return None
    return (
        "; ".join(problems)
        + " Receipt facts: identity fields actually surfaced by journaled tool results: "
        + json.dumps(receipt_identity_keys(tool_results), ensure_ascii=False)
        + "; cited identity_keys: "
        + json.dumps([paper.identity_key for paper in out.papers], ensure_ascii=False)
        + "."
    )
