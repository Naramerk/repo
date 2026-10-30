cleanup(ext-benchmodels): all 19 ``Any`` occurrences removed in
tests/test_bench_models.py; the file had no getattr/cast/type: ignore/audit-ok
and gains none.

The four payload helpers (``_quality_payload``/``_metrics_payload``/
``_task_payload``/``_manifest_payload``) and the six parametrized ``rejects``
signatures now use ``object`` instead of ``Any`` (``**overrides: object``,
``dict[str, object]``): these heterogeneous dicts flow only into pydantic's
``model_validate(obj: Any)`` and back into helper kwargs — unlike a typed
constructor call there is no ``**``-unpacking into declared fields, so
``object`` type-checks cleanly (mypy on the file passes; tests/ is outside the
CI mypy scope). Annotations have no runtime effect; all 31 tests pass
unchanged. Callers verified via CodeGraph: the helpers are file-local, no
other module imports them.

Kept as-is: ``setattr(task, field_name, "other")`` in
``test_bench_task_is_frozen`` — outside this task's marker scope
(Any/getattr/type: ignore/cast), not an audited counter, kept byte-for-byte
from base.

Counts in tests/test_bench_models.py (вхождения, base fde271e -> head):
Any 19 -> 0, getattr 0 -> 0, type: ignore 0 -> 0, cast 0 -> 0.
