cleanup(finalized-tests): tests read the public ``runtime.is_finalized`` seal flag
- ``tests/``: every read of the private ``StudyRuntime._finalized`` attribute —
  17 ``getattr(rt, "_finalized", False)`` sites and 46 direct ``rt._finalized`` /
  ``rt2._finalized`` reads across 16 test files — now uses the public
  ``is_finalized`` property introduced in #389. The property returns
  ``self._finalized`` verbatim (``machine.py``), and ``machine`` precedes
  ``LifecycleMixin`` in every runtime MRO (api / experimental), so every assert
  and branch keeps its exact semantics.
- ``tests/checks/test_quote_in_source.py``: the stale ``# noqa: SLF001`` on the
  replaced read is gone together with the private access it silenced.
- ``tests/test_outcome.py``: the closed-attempt test's untyped
  ``get_task=lambda _: None`` fake becomes a typed ``Callable[[str], Task]``
  guard (``_no_task``) that fails loudly if a closed attempt ever reaches task
  resolution — ``Executor.record`` raises ``AttemptClosedError`` before calling
  ``get_task``, so the passing path is identical (#389 follow-up; same
  guard-stub pattern as ``tests/api/test_contract_views.py``).
- ``coscientist/core/plan/lifecycle.py``: docstring added to the
  ``LifecycleMixin.is_finalized`` typing stub only — code untouched (#389
  follow-up).
