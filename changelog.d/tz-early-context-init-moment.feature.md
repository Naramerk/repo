feature(report): the ГОСТ 19.201-78 ТЗ renders at plan acceptance (legacy context_init moment)
- ``coscientist/cli/report_lane.py``: new ``render_tz_early`` renders the ТЗ
  right after plan acceptance and CASes it; ``render_report_docs`` accepts
  ``tz_artifact_id`` and skips its ТЗ model call when the document already
  exists (fallback path without the id renders as before). The ТЗ documents
  the ASSIGNMENT (tasks, criteria, questions) — the plan digest carries all
  of it at acceptance, and the legacy CoScientist issued it at
  ``context_init`` before any work; rendering it at the end spent the run's
  critical tail on a result-independent document (36-minute report phase in
  live st_8eb26d73 vs the hour budget). An early refusal is never fatal —
  usage-logged, the end-of-run lane owns the render.
- ``coscientist/cli/run.py``: the wired report lane renders the ТЗ once
  after the plan is accepted and threads the artifact id through the report
  gate into both renders.
- Operator presets: ``deadline_s`` 3600 → 4500 (owner grant 2026-10-31:
  «можно чуть больше часа» — headroom for the reduced-but-not-free report
  phase).
- Document moments now match the legacy: ТЗ at the start (from the
  assignment), science report + НИР 7.32 at the end (from the results).
- Still open for ``fulfilled`` on the live task class: structural
  hypothesis claims (universal-acceptance #273).
