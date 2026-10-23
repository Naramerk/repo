"""Pure M0 state core: domain, events, store, machine, executor (no ADK/web).

Beyond-M0 surfaces (HITL gates, DelegatedBudget/costly_gate, report) live in
``coscientist.experimental``. Requirement catalog is ``coscientist.checks`` (M1).
"""

from coscientist.core.claims import rules as rules

__all__ = ["rules"]
