"""Agent wiring package (DESIGN_M1 §8).

Agents must call tools only via ``coscientist.tools.invoke_tool``. Direct
``Tool.call``, ``register_tool``, and imports of ``tools.contract`` /
``core.tool_registry`` / ``core.dispose`` / ``core.artifact_stamp`` are
forbidden (import-linter + AST tests).
"""

from coscientist.agents.research_spec import (
    Constraint,
    Goal,
    ResearchSpec,
    SuccessMetric,
)

__all__ = [
    "Constraint",
    "Goal",
    "ResearchSpec",
    "SuccessMetric",
]
