# Coder workflow attach (`invoke_coder_for_study`)

Study path for CAP #11 / DESIGN §6.4: hard sandbox preflight, then
`invoke_tool(coder)` → `run_coder_job` (bubblewrap guest + JobStore).

## Entry

```python
from coscientist.tools.coder import invoke_coder_for_study

outcome, tool_out = await invoke_coder_for_study(
    request,  # CoderRequest (limits.allow_cgroup_degraded must be False)
    jobs=jobs,
    writer=writer,
    artifacts=artifacts,
    ctx=ctx,  # CallCtx with matching attempt_id; side_effect allow required
)
```

Registry name: `coder` (`provider=coder`, `effect=write`) via
`ensure_core_tools_ready` / `CORE_BUILTIN_TOOLS`.

## Preflight (fail-closed)

`require_supported_linux_sandbox` refuses before spawn when:

| Gate | Refuse detail (substring) |
|------|---------------------------|
| no bwrap | `bwrap unavailable` |
| soft net/fs isolation | `isolation not hard` |
| no / bad `CGROUP_JOB` | `cgroup unavailable` |
| `allow_cgroup_degraded=True` | `allow_cgroup_degraded bypass forbidden` |

No isolation bypass on this path. Backend gorilla closes from #150
(`CgroupEventsLiveness` / `GuestProcessGroupGone` journal+raise) are unchanged
inside `run_coder_job`.

## Hard deps (do not invent)

| Missing | Honest behavior |
|---------|-----------------|
| `ToolResolved(status=coder\|tool_built)` on #65 tip | Only `tool_found` / `tool_missing` today. Do **not** invent `status=coder` from `tool_missing`. |
| Alembic fallback + profile `alembic:` policy | Not on #150 tip; DESIGN §6.3 steps 2–3 remain deps. |
| Executor-route → coder via AgentPort (`#146`/`#156`) | Adapter still fail-closes on non-`tool_found`. Wire after catalog/status exist. |
| Production `JobStore` → `StudyWriter` | `InMemoryJobStore` is the contract store; journal adapter is a follow-up (`jobs.py`). |

When those land, callers may journal a real coder resolution then call
`invoke_coder_for_study`. Until then, missing catalog/role routing must refuse.
