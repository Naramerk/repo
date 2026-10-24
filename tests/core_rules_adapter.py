"""Adapter: core rules + experimental costly + refmodel Graph/Claim for impl=core.

Graph / Claim / add_paper stay in the independent oracle; validate_plan /
check_cardinality / claim_stance exercise ``core.rules`` by duck typing.
``verdict`` is ``hypothesis_verdict`` directly (no second norm).
DelegatedBudget / costly_gate / settle come from ``coscientist.experimental``
(pre-M2), not from the M0 core API.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import refmodel

from coscientist.core.claims.verdict import hypothesis_verdict


def make_core_impl() -> Any:
    """Build an impl surface: core rule functions + experimental costly + refmodel Graph/Claim."""
    import coscientist.core.claims.dispositions as dispositions
    import coscientist.core.claims.rules as core
    import coscientist.core.events.errors as errors
    import coscientist.core.finalize.finalize as finalize
    import coscientist.core.finalize.study_view as study_view
    import coscientist.experimental.costly as costly

    def terminal_reason_for(trigger, *, tasks=None, question_dispositions=None):
        tr = finalize.terminal_reason_for(
            trigger,
            tasks=tasks or {},
            question_dispositions=question_dispositions or {},
        )
        return getattr(tr, "value", tr)

    return SimpleNamespace(
        Graph=refmodel.Graph,
        Claim=refmodel.Claim,
        edge_allowed=core.edge_allowed,
        PlanInvalid=core.PlanInvalid,
        validate_plan=core.validate_plan,
        check_cardinality=core.check_cardinality,
        claim_stance=core.claim_stance,
        verdict=hypothesis_verdict,
        study_fulfilled=core.study_fulfilled,
        study_view_verdict=core.study_view_verdict,
        pick_finalize_trigger=finalize.pick_finalize_trigger,
        terminal_reason_for=terminal_reason_for,
        study_phase=study_view.study_phase,
        not_attempted_reason=dispositions.not_attempted_reason,
        reject_write_after_finalized=errors.reject_write_after_finalized,
        DelegatedBudget=costly.DelegatedBudget,
        costly_gate=costly.costly_gate,
        settle=costly.settle,
    )
