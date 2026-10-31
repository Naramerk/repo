fix(derive): falsifiable questions upgrade hypotheses.needed instead of refusing
- ``coscientist/agents/derive.py``: ``claim_class=falsifiable`` with no
  verdict engine (no substantive catalogue criterion, ``hypotheses.needed``
  off) no longer refuses derivation — derive UPGRADES the Need (recorded in
  ``PlanOut.need_upgrades``) and routes the question ``hypothesis_claimed``.
  The engine is a system guarantee (#314 layer), not a planner promise: GLM
  flips Need off even on verdict-bearing tasks and one replan round never
  converges it — live st_idem_ecb5b370 q5 (wave-18) and st_51e0ddb836a72bc5
  q2 (2026-10-31 run 4) both died on the old refusal. The RC4 contract is
  intact: accept sees Need.on, inserts HYP-n, and a falsifiable claim is
  still never closed on bare receipts; ``require_hypotheses`` profiles keep
  refusing ``needed=false`` plans at accept, so the upgrade cannot hide a
  profile violation. The value/review honesty rails and the legacy pre-S1
  refusal for absent claim_class are untouched.
- ``coscientist/agents/schemas.py``: ``PlanOut.need_upgrades`` (system-set,
  never model-emitted — same channel as ``uncitable_drops``).
- ``coscientist/core/plan/lifecycle.py``: ``accept_plan`` journals the
  upgrade as a readable note (``derive_need_upgrade_note``) in the
  PlanAccepted system-note chain.
- ``coscientist/cli/run.py``: passes ``plan_out.need_upgrades`` through to
  ``accept_plan``.
- Tests updated to the new contract: universal-acceptance falsifiable
  refusal → upgrade assertions; catalog-v5 falsifiable param split into the
  legacy None refusal + upgrade test; new lifecycle note test in
  tests/test_rc4_hyp_n.py.
