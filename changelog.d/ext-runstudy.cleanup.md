tests/cli/test_run_study.py typed cleanup, behavior unchanged (annotation-only).
Exact marker counts per file (вхождения, grep -o | wc -l), base
origin/cleanup/refactor (94e7643) → this branch: ``Any`` 0 → 0, ``getattr``
0 → 0, ``type: ignore`` 12 → 6, ``cast(`` 0 → 0, ``audit-ok`` 0 → 0.
Removed: three dead ``# type: ignore[index]`` on ``_coder_job_payload()["EXP-0"]``
indexing (mypy ``--warn-unused-ignores`` reported them unused; identical
indexing elsewhere in the file never needed them); ``# type: ignore[arg-type]``
on ``_ArgsGapSurface.planned_calls`` and on ``tools=surface`` (the fake now
declares its real types — ``named_tools() -> Sequence[NamedTool]``,
``planned_calls(hints: Sequence[str]) -> tuple[PlannedToolCall, ...]`` — and
structurally satisfies the ``CliToolSurface`` protocol, so mypy checks it
directly); ``# type: ignore[arg-type]`` on the ``_track_start`` wrapper
(``**kwargs`` retyped from ``object`` to ``Provenance | None`` to match the
exact ``StudyRuntime.start_attempt`` keyword, so forwarding typechecks).
Kept (each suppresses a real mypy error; removing it would require a cast,
an API change, or a behavior change): three ``# type: ignore[arg-type]`` on
``adapter=`` where a minimal duck-typed ``_SpyAdapter`` fake is passed to
``_execute_executor_attempt`` whose parameter is the concrete
``LlmAgentAdapter`` class — subclassing it would drag base-class state into
the fake and a Protocol parameter would change the ``run.py`` API;
``# type: ignore[arg-type]`` on ``loaded_profile="research"`` (deliberate
wrong-type input driving the fail-closed "loaded_profile must be Profile"
refuse); two ``# type: ignore[arg-type]`` on ``_dispatch_coder_task(None, ...,
writer_journal=None, ...)`` (deliberate ``None`` inputs reaching the typed
backstop refuse before those arguments are used). Verification: mypy on the
file 58 → 55 errors with zero new ones (tests/ is outside the mypy CI scope;
the remainder are pre-existing), ruff check clean, pytest
5 failed / 72 passed identical to base (the 5 are baseline local-environment
``ModuleNotFoundError: httpx``).
