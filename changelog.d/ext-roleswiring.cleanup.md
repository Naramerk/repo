tests/cli/test_roles_wiring.py: removed all 13 ``type: ignore`` (вхождения,
grep -o; 1 ``ignore[arg-type]``, 7 ``ignore[union-attr]``, 5 ``ignore[index]``)
with zero behavior change. Counters for the file, base → head (вхождения):
``type: ignore`` 13 → 0, ``Any`` 0 → 0, ``getattr`` 0 → 0, ``cast(`` 0 → 0,
``audit-ok`` 0 → 0 (verified with counts.sh word-boundary patterns; tests/
stand outside mypy CI scope, so the file was type-checked separately).

How each class of ignore went away:

* ``ignore[arg-type]`` (``_critic_body``): the parameter is now annotated
  ``CriticDecision`` (the public ``Literal["accept", "revise", "reject"]`` from
  ``coscientist.agents.critic``; all three callers pass ``"accept"`` /
  ``"revise"`` literals — CodeGraph callers checked), and the issues tuple now
  constructs ``CriticIssue(path=..., message=...)`` explicitly — identical
  validated model and identical ``model_dump_json()`` bytes as the previous
  nested-dict validation.
* 12 ``ignore[union-attr]`` / ``ignore[index]``: all cascaded from
  ``_run_wired`` returning ``dict[str, object]``. The return is now a private
  ``TypedDict`` (``_RunSummary`` / ``_UsageRow``) covering only the keys the
  tests assert on. Every source conforms (rule checked at the emission sites,
  read via LeanCTX): ``role`` is ``str`` in every usage row run_study emits
  (``_usage_row``, critic journal row, reviewer row, hitl/report rows),
  ``prompt_tokens`` is ``int`` (``llm.types.Usage``), the critic journal row
  carries ``stage: str`` / ``issues: list[str]`` / ``note: str``, the reviewer
  row carries ``artifact_ids: dict[str, str]`` (``set()`` reads its keys) and
  ``gaps: list[str]``. Runtime assertions, call order, and expected outcomes
  are unchanged; no ``isinstance`` filter was added or removed.
* One non-marker type fix: the local ``sleep`` closure parameter ``_delay`` is
  renamed ``delay_s`` to satisfy the ``AsyncSleep`` protocol's
  positional-or-keyword ``__call__(self, delay_s: float)`` (mypy 2.4.0 matches
  keyword names for protocol callables); runtime behavior is identical.

No markers were left behind in this file (nothing needed an audit-ok). No
unrelated lines were reformatted: ``ruff format --check`` on the base file
fails with the same 11 pre-existing join-hunks (CI runs ``ruff check`` only).
