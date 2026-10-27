# Per-question acceptance coverage (legacy semantics port)

Date: 2026-10-27. Branch: `lab/acceptance-question-coverage` (base `14133e8`).
Source analysis: legacy acceptance semantics read-only review (`/tmp/legacy-acceptance-semantics.md`,
sections 2.3, 3, 6.3).

## Legacy reference

In legacy CoScientist the two acceptance loops are separate (see the legacy
review, §2.3 and §6.3):

- **A question closes on grounded evidence, not on a verdict.**
  `research_quality/completion.py` doctrine: "A report does not close an open
  question, hypothesis, or research task"; a `ResearchQuestion` is closed
  exactly when a non-rejected `Evidence` node with non-empty content relates
  to it (`completion.py:35–59, 89–92`). No verdict is required to close a
  question.
- **The verdict belongs to hypotheses, not to the question form.**
  Whether a verdict is needed at all is decided by the class of the claim
  (`hypotheses/jev.py:44–80`); verdicts are written per `Hypothesis` against
  `ConfirmationCriteria` by an independent background judge
  (`graph/research/validator.py::judge_hypothesis`), never by the task
  executor and never by the orchestrator (`queries.py:411–416`).
- **Tasks that only gather evidence carry no scientific verdict
  requirements.** LIT tasks were accepted on (a) a named report artifact,
  (b) registration integrity, (c) criteria the plan declared, if any,
  (d) grounded evidence (`legacy review §2.2`). The judge then classifies
  that evidence's polarity for the hypothesis.

So the legacy invariant is: **every verdict-bearing question must eventually
receive grounded evidence; the scientific bar sits on the claim that consumes
the evidence, not on every task that produces it.**

## The problem being fixed (attempts 15/16 walls)

Before this port, `derive_task_specs` enforced the substantive-catalogue bar
**per task**: every task addressing a `verdict`/`both` question had to cite a
substantive catalogue criterion. That is stricter than the legacy invariant
and self-defeating for evidence-gathering deliveries:

- a literature-grounding task (attempt 15) has no computation tool output to
  pin a `json_pointer`/`metric_threshold` check to;
- a synthesis-cost scan (attempt 16) likewise.

Any plan that paired such an evidence task with a real computation task on
the same question was refused, because the *evidence* task alone failed the
per-task bar. The only way through was to drop the evidence task or to
fabricate an inapplicable criterion — both wrong.

## Our contract (post-port)

`coscientist/agents/derive.py::derive_task_specs` now enforces the bar at the
**question level**:

1. Every question still needs an explicitly addressing task (unchanged).
2. For every `verdict`/`both` question, **at least one** addressing task must
   cite a substantive catalogue criterion — same substantive definition as
   before: `required` + `CodeCheck` with a content-binding kind
   (`json_pointer`, `value_matches`, `metric_threshold`, `quote_in_source`,
   `exec_runs`, `tests_pass`, `metric_on_holdout`, `reproducible_rerun`).
3. Additional co-addressing tasks may carry no criteria: they deliver
   **evidence** through their auto-registered output rubrics
   (`artifact_exists` on the tool-result artifact — an existence check, never
   promoted to scientific truth).
4. The refusal is **per question**: it names the question id, its text and
   final type, and the addressing tasks that failed to cover it
   (`question 'q2' (...) is not covered: addressing tasks [...] cite no
   substantive catalogue check ...`).
5. Per-task checks are unchanged: unknown tools/dependencies/keys, duplicate
   criteria, and criterion→output pin validation (`criterion references
   undeclared output`) stay per task at derive; criterion pins stay validated
   at schema parse (`plan_intent_model_for`), feeding the targeted repair
   round.

Catalogue addition: `bench/lab/glm/checks_catalog_v5.json` gains
`heracleum_synthesis_cost_estimate` — a `json_pointer` pin to
`heracleum-tox:estimate_synthesis_cost` at
`/result/structuredContent/answer`. The tool's answer shape is not confirmed
upstream, so the template honestly checks **presence of machine-readable
evidence** only (non-empty `structuredContent.answer`); it does not assert
specific fields or the truth of a cost estimate (text_ru says exactly that).
v4 catalog is untouched.

## Why this is not a gate weakening

- **A verdict question with zero substantive tasks refuses exactly as
  before.** The gate moved from "every task" to "at least one task per
  question"; a plan where no addressing task carries a substantive criterion
  still fails derivation, now with a message naming the uncovered question.
- **Route strings do not help.** The gate inspects only cited catalogue
  templates (required + code-checked + content-binding kind). Route, goal and
  delivery wording are not consulted; a non-substantive citation
  (`artifact_present` class) still cannot cover a verdict question
  (tested: `test_evidence_only_coaddressing_cannot_substitute_criterion_with_route_strings`).
- **Substitution of route strings for criteria is impossible by
  construction**: criteria come from the catalogue enum on the study surface;
  free-form criterion text is refused at schema parse.
- **Every verdict submission keeps its substantive check.** The covering
  task's criterion is evaluated as before (`evaluate_code_check`, honest
  `pointer_empty`/`output_not_submitted` FAILED handling, no numeric effect
  on indeterminate). Co-addressing tasks add only existence-rubric evidence;
  artifact existence is never accepted as a scientific verdict
  (`tests/agents/test_scientific_acceptance.py::test_empty_content_cannot_be_answered_by_met_artifact_rubric`).
- **RC5/RC6 and the e2e suite are untouched**, as are `accept_plan` freeze
  gates (the derive gate remains the first, fail-closed owner; the runtime
  gates stay the backstop).

## Known doc drift (coordinator-owned)

`docs/plan/scientific-acceptance.md` still states the pre-port per-task bar
("For `verdict` and `both`, each addressing task needs a required substantive
catalogue check."). Updating it is outside this branch's file bounds; the
sentence should become the per-question rule above during review/merge.

## Tests

- `tests/agents/test_scientific_acceptance.py` — attempt-15 shape passes for
  `verdict` and `both`; uncovered verdict question refuses naming the
  question; refusal names only the uncovered question; non-substantive
  citation cannot cover.
- `tests/checks/test_catalog_v5_bench.py` — attempt-12 refusal stays (per
  question); evidence-only co-addressing passes; `heracleum_synthesis_cost_estimate`
  is satisfiable on the heracleum surface, substantive at derive, and
  evaluates honestly (MET on machine-readable answer, `pointer_empty` FAILED
  on text-only/error receipts).
