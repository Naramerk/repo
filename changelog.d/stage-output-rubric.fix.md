fix(plan): the #327 stage output carries the artifact-exists rubric
- ``coscientist/core/plan/lifecycle.py``: ``_stage_expected_outputs`` (the
  #327 guarantee — the stage output declared on hypotheses-route tasks and
  the intake DAG) now attaches the single-item artifact-exists rubric, the
  same shape derive attaches to planner tool-id outputs. The disposition
  engine's ``task_rubric_status`` sees only rubrics declared IN the spec:
  with ``rubric=null`` the attempt's default validity gate fired (MET in the
  result row) but was invisible to acceptance — live st_4f522351 2026-10-31:
  an artifact question covered by a complete hypotheses task stayed
  ``not_attempted`` and capped the study at ``partial`` with every required
  task SUCCESS and the report written. Digest is frozen by
  ``freeze_task_rubrics`` on accept, same as planner-declared rubrics.
- Remaining barrier for fulfilled on the live task class: verdict-type
  questions need structural hypothesis claims (the verdict engine counts
  ``agent_claim``s; live lanes record none — universal-acceptance #273).
