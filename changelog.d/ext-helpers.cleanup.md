tests/helpers.py: narrowed provable types and dropped provable getattr
without behavior change. ``RuntimeLike`` members now carry their production
types (``writer: StudyWriter``, ``clock: Clock``, ``tasks: dict[str, Task]``,
``executor: Executor | None``) and ``_project_local`` takes ``EventEnvelope``
(matching both real implementations in ``local_project.py`` and
``experimental/runtime.py``); ``open_runtime`` / ``open_runtime_experimental``
return ``StudyRuntime`` as their factories do. Removed 3 ``getattr`` whose
attribute is guaranteed by the type (``rt.executor`` is a Protocol member set
in ``machine.StudyRuntime.__init__``; ``Executor.artifacts`` and
``Executor.get_graph`` are always assigned in ``Executor.__init__``); 7 ``Any``
dropped.

Kept markers, each verified live (not removable without behavior/contract
change):

- ``getattr(rt, "artifacts", None)`` — ``artifacts`` is not a ``RuntimeLike``
  member (capability probe); the default is reachable for objects that do not
  declare it.
- ``getattr(rt, "make_executor", None)`` / ``callable(make)`` and
  ``getattr(rt, "bind_executor", None)`` / ``callable(bind)`` — optional
  capability probes with real alternative branches.
- ``getattr(rt, "study", None)``, ``getattr(rt, "max_attempts", 0)``,
  ``getattr(rt, "_check_proposals", {})`` — not ``RuntimeLike`` members;
  defaults are reachable for objects without these attributes
  (``_check_proposals`` is even created lazily in production code).
- ``ex._runtime_attempts = rt.attempts  # type: ignore[attr-defined]`` —
  mirrors ``machine.py`` production wiring; the attribute is read back via
  ``getattr`` in ``executor.py`` and is intentionally not declared on
  ``Executor``.
- Remaining ``Any`` (``Mapping[str, Any]`` payloads, ``dict[str, dict[str,
  Any]]`` attempts, ``catalog: Any | None``, ``**kwargs: Any``) — inherited
  verbatim from the production signatures they forward to
  (``machine.plans``/``machine.attempts``, ``runtime_factory.open_runtime``).
