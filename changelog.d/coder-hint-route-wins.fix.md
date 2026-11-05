fix(plan): route wins over the coder tools_hint — accept strips it off stage routes
- ``coscientist/core/plan/lifecycle.py``: a plan draft combining a stage route
  (LIT / HYP / PLAN) with the reserved ``coder`` tools_hint survived plan
  acceptance and killed the whole run at dispatch (live st_d705d826
  2026-10-31: planner emitted a hypotheses-route verdicts task with
  ``tools_hint ['coder']`` — the coder dispatch typed-refused mid-run with
  ``code execution is experiment delivery only``; the study had 7/9 tasks
  DONE). ``accept_plan`` now strips the hint off stage-route tasks (same
  system-guarantee layer as the RC4 HYP-n insert and the hyp stage-output
  declare): the route decides the delivery lane, the drop is journaled as a
  readable ``PlanAccepted`` note (``coder_hint_drop_note``), payload parity
  preserved when nothing changed. The dispatch-side typed refuses stay as
  defense in depth.
- ``coscientist/core/execution/domain.py``: the canonical ``CODER_HINT``
  ("coder") moves into the task domain, next to ``TaskSpec.tools_hint`` —
  core-side acceptance guarantees key on it without importing the agents
  layer (import-linter: core must not import ``coscientist.agents``).
  ``coscientist/agents/derive.py`` re-exports the same name; no call-site
  churn.
- The expansion entry point (``validate_expansion``) does not normalize yet —
  expansion-drafted stage tasks with the coder hint still refuse at dispatch;
  follow-up if live plans ever draft expansions with the hint.
