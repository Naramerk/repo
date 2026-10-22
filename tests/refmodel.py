"""Executable reference of RESEARCH_MODEL.md rules (validator level).

Implementation lives in `coscientist.core.rules`. This module re-exports it so
`impl=refmodel` stays a stable import path for shared tests.

Oracle note: there is no divergent logic here — core.rules is the single source.
Keep this file only as the documented test entrypoint / alias; add independent
oracle helpers here only if a future rule must be pinned *against* core.
"""

from coscientist.core.rules import (  # noqa: F401
    PAIRS,
    SCHEMA,
    Claim,
    DelegatedBudget,
    Graph,
    PlanInvalid,
    check_cardinality,
    claim_stance,
    costly_gate,
    edge_allowed,
    settle,
    study_fulfilled,
    study_view_verdict,
    validate_plan,
    verdict,
)

__all__ = [
    "SCHEMA",
    "PAIRS",
    "Claim",
    "DelegatedBudget",
    "Graph",
    "PlanInvalid",
    "check_cardinality",
    "claim_stance",
    "costly_gate",
    "edge_allowed",
    "settle",
    "study_fulfilled",
    "study_view_verdict",
    "validate_plan",
    "verdict",
]
