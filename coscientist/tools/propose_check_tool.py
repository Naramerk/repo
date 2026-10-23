"""Agent entry point for ``proposed_check`` (DESIGN_M1 §16 / PR20).

Agents call this via ``invoke_tool`` only. The tool preregisters a
``CheckProposed`` and returns ``proposal_id`` for PlanAccepted / PlanExpanded
criteria: ``RequirementRef(template_id='proposed_check', params={proposal_id})``.
"""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import CodeCheck, RejectedTemplateNote
from coscientist.core.proposed_check import propose_check, requirement_ref_for_proposal
from coscientist.core.rubric_types import Rubric
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec


class ProposeCheckTool:
    """Write-effect tool: preregister an agent-proposed catalog meta-check."""

    spec = ToolSpec(
        name="propose_check",
        effect="write",
        timeout_s=30,
        max_retries=0,
        provider="local",
        input_schema={
            "type": "object",
            "required": [
                "target",
                "role",
                "measures",
                "justification",
                "rejected_templates",
                "task_id",
            ],
            "properties": {
                "target": {"type": "string"},
                "role": {"enum": ["prediction", "validity"]},
                "measures": {"type": "string"},
                "justification": {"type": "string"},
                "rejected_templates": {"type": "array"},
                "task_id": {"type": "string"},
                "check": {"type": "object"},
                "rubric": {"type": "object"},
                "open_hitl": {"type": "boolean"},
            },
        },
    )

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        runtime = (ctx.extra or {}).get("runtime")
        if runtime is None:
            raise ValueError("propose_check tool requires ctx.extra['runtime']")
        aid = str(ctx.attempt_id or "")
        if not aid:
            raise ValueError("propose_check tool requires ctx.attempt_id")
        attempts = getattr(runtime, "attempts", None) or {}
        att = attempts.get(aid) if isinstance(attempts, dict) else None
        if not isinstance(att, dict):
            raise ValueError(f"propose_check: unknown ctx.attempt_id {aid!r}")
        bound_task = str(att.get("task_id") or "")
        arg_task = str(args["task_id"])
        if not bound_task or arg_task != bound_task:
            raise ValueError(
                f"propose_check task_id {arg_task!r} does not match "
                f"ctx.attempt_id task {bound_task!r}"
            )
        notes = [
            RejectedTemplateNote.model_validate(n) if isinstance(n, dict) else n
            for n in (args.get("rejected_templates") or [])
        ]
        check = args.get("check")
        rubric = args.get("rubric")
        pid = await propose_check(
            runtime,
            target=str(args["target"]),
            role=args["role"],
            measures=str(args["measures"]),
            justification=str(args["justification"]),
            rejected_templates=notes,
            task_id=bound_task,
            attempt_id=aid,
            check=CodeCheck.model_validate(check) if check else None,
            rubric=Rubric.model_validate(rubric) if rubric else None,
            open_hitl=bool(args.get("open_hitl", True)),
        )
        if pid is None:
            return ToolOutput(data={"proposal_id": None, "reason": "proposal_quota"})
        ref = requirement_ref_for_proposal(pid, target=str(args["target"]))
        return ToolOutput(
            data={
                "proposal_id": pid,
                "criterion": ref.model_dump(mode="json"),
            }
        )


PROPOSE_CHECK_TOOL = ProposeCheckTool()
