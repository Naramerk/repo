cleanup(ext-roleswiring2): marker-free ``tests/cli/test_roles_wiring.py``.

Marker counts for the file (вхождения, ``git grep -o | wc -l``), before →
after: ``type: ignore`` 12 → 0, ``Any`` 0 → 0, ``getattr`` 0 → 0, ``cast`` 0 → 0,
``audit-ok`` 0 → 0. No markers remain in the file.

``_run_wired`` now returns ``tuple[int, _StudySummary]``. ``_StudySummary`` is a
module-level ``TypedDict`` describing the run-summary JSON printed by
``run_study`` (coscientist/cli/run.py: ``study_id``/``phase``/``verdict``/
``quality``/``usage``); ``_UsageRow`` (required ``role``, optional keys) describes
the usage-row shapes these runs emit: ``_usage_row`` rows
(coscientist/cli/study_helpers.py), the wired-critic journal row
(coscientist/cli/run.py) and the wired-reviewer lane row
(coscientist/cli/review_lane.py, where ``artifact_ids`` is ``dict[str, str]``).
``TypedDict`` is static-only, so runtime behavior is unchanged; all 12 ignore
comments (8 union-attr on iterating ``summary["usage"]``, 4 index on usage rows)
became unnecessary. The local no-op ``sleep`` helper parameter was renamed
``_delay`` to ``delay_s`` to match the ``AsyncSleep`` protocol keyword name
(coscientist/llm/runtime.py); the client calls ``sleep`` positionally
(coscientist/llm/client.py), so this is a pure rename with no behavior change.
mypy 1.19.1 on this test module (outside the CI mypy scope), default and
``--check-untyped-defs``: no new errors; the base errors in this module are gone.

Call chains were verified with CodeGraph (``codegraph query`` for run_study /
summary / usage-row shapes, ``codegraph node Usage``, ``codegraph callers``) and
LeanCTX anchored reads (``lean-ctx read -m lines`` / ``-m signatures``) of
coscientist/cli/run.py, coscientist/cli/study_helpers.py,
coscientist/cli/review_lane.py, coscientist/llm/runtime.py and
coscientist/llm/types.py.
