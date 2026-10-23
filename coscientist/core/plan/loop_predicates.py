"""Loop until-predicates (M1 PR11 / S10).

Built-ins see only ``scope_*`` and claims/verdicts from tasks of the same
``loop_id``. Plugins register via ``register_loop_predicate`` (cannot override
built-ins).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any, Protocol

from coscientist.core.claims.claims import claims_for_ref, filtered_claims_by_target
from coscientist.core.claims.reasons import Disposition
from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.claims.rules import verdict as verdict_fn
from coscientist.core.claims.verdict import disposition_for_question
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.execution.domain import LoopPredicate, LoopSpec, ReqStatus, Study

# survivors_le n is a count threshold — keep well below pathological ints.
SURVIVORS_LE_N_MAX = 10_000


class LoopPredicateFn(Protocol):
    def __call__(
        self,
        runtime: Any,
        loop: LoopSpec,
        *,
        iter_n: int,
        ctx: Mapping[str, Any],
    ) -> bool: ...


_REGISTRY: dict[str, LoopPredicateFn] = {}
BUILTIN_LOOP_PREDICATES: frozenset[str] = frozenset(
    {"survivors_le", "criterion_met", "no_change"}
)


class UnknownLoopPredicateError(PlanInvalid):
    """Unknown until.name — fail-closed at accept / eval."""


class LoopPredicateEvalError(Exception):
    """Expected fail-closed eval failure (unknown/unregistered predicate)."""


def normalize_predicate_name(raw: str) -> str:
    """NFKC-normalize predicate name; reject empty / invisible chars."""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not s:
        raise PlanInvalid("LoopSpec.until.name is required")
    if any(unicodedata.category(ch) in {"Cf", "Cc", "Cs"} for ch in s):
        raise PlanInvalid(f"loop predicate name has invisible/control chars: {raw!r}")
    return s


def register_loop_predicate(name: str, fn: LoopPredicateFn) -> None:
    """Register a loop predicate. Built-ins and existing plugins cannot be overridden."""
    key = normalize_predicate_name(name)
    if key in BUILTIN_LOOP_PREDICATES:
        if key in _REGISTRY:
            raise ValueError(f"cannot override built-in loop predicate: {key!r}")
        # First registration of a built-in (module load) is allowed.
    elif key in _REGISTRY:
        raise ValueError(f"cannot override registered loop predicate: {key!r}")
    _REGISTRY[key] = fn


def known_loop_predicates() -> frozenset[str]:
    return frozenset(_REGISTRY)


def get_loop_predicate(name: str) -> LoopPredicateFn | None:
    try:
        return _REGISTRY.get(normalize_predicate_name(name))
    except PlanInvalid:
        return None


def eval_loop_predicate(
    runtime: Any,
    loop: LoopSpec,
    *,
    iter_n: int,
    ctx: Mapping[str, Any] | None = None,
) -> bool:
    """Evaluate ``loop.until``. Unknown name → LoopPredicateEvalError; body bugs propagate."""
    try:
        key = normalize_predicate_name(loop.until.name)
    except PlanInvalid as e:
        raise LoopPredicateEvalError(str(e)) from e
    fn = _REGISTRY.get(key)
    if fn is None:
        raise LoopPredicateEvalError(f"unknown loop predicate: {key!r}")
    return bool(fn(runtime, loop, iter_n=iter_n, ctx=ctx or {}))


def _require_int_n(params: dict[str, Any]) -> int:
    """survivors_le params.n: exact int, not bool, 0 <= n <= SURVIVORS_LE_N_MAX.

    Never coerce via ``float()`` (OverflowError on huge ints).
    """
    if "n" not in params:
        raise PlanInvalid("survivors_le requires params.n")
    n = params["n"]
    if isinstance(n, bool) or not isinstance(n, int):
        raise PlanInvalid(
            f"survivors_le params.n must be int >= 0 (got {type(n).__name__})"
        )
    if n < 0:
        raise PlanInvalid("survivors_le params.n must be >= 0")
    if n > SURVIVORS_LE_N_MAX:
        # Do not interpolate ``n`` — huge ints blow f-string formatting.
        raise PlanInvalid(
            f"survivors_le params.n must be <= {SURVIVORS_LE_N_MAX}"
        )
    return n


def validate_loop_predicate(pred: LoopPredicate | dict[str, Any] | None) -> None:
    """Raise PlanInvalid if predicate missing/unknown or params invalid."""
    if pred is None:
        raise PlanInvalid("LoopSpec.until is required")
    params: dict[str, Any]
    if isinstance(pred, dict):
        name = normalize_predicate_name(str(pred.get("name") or ""))
        raw_params = pred.get("params")
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
    else:
        name = normalize_predicate_name(str(pred.name or ""))
        params = dict(pred.params or {})
    if name not in _REGISTRY:
        raise UnknownLoopPredicateError(f"unknown loop predicate: {name!r}")
    if name == "survivors_le":
        _require_int_n(params)
    elif name == "criterion_met":
        cid = params.get("criterion_id")
        if not cid or not str(cid).strip():
            raise PlanInvalid("criterion_met requires params.criterion_id")


def loop_task_keys(runtime: Any, loop_id: str, *, upto_iter: int | None = None) -> set[str]:
    """Task keys belonging to ``loop_id`` (optionally ``loop_iter <= upto_iter``)."""
    keys: set[str] = set()
    for t in getattr(runtime, "tasks", {}).values():
        spec = t.spec
        if getattr(spec, "loop_id", None) != loop_id:
            continue
        it = getattr(spec, "loop_iter", None)
        if upto_iter is not None and (it is None or int(it) > int(upto_iter)):
            continue
        keys.add(spec.task_key)
    return keys


def _claims_for_loop_scope(runtime: Any, loop: LoopSpec, *, upto_iter: int) -> dict[str, list]:
    """Claims about scope targets produced by this loop's tasks (upto iter)."""
    graph = getattr(runtime, "graph", None)
    study = getattr(runtime, "study", None)
    if graph is None or study is None:
        return {}
    loop_keys = loop_task_keys(runtime, loop.loop_id, upto_iter=upto_iter)
    arts = getattr(runtime, "artifacts", None)
    lookup = getattr(arts, "get_meta", None) if arts is not None else None
    by_target = filtered_claims_by_target(
        graph,
        study_id=study.study_id,
        results=getattr(runtime, "results", None),
        artifact_lookup=lookup if callable(lookup) else None,
    )
    scoped: dict[str, list] = {}
    for ref in list(loop.scope_hypotheses) + list(loop.scope_questions):
        prefer_h = ref in loop.scope_hypotheses
        claims = claims_for_ref(
            by_target,
            ref,
            prefer="hypothesis" if prefer_h else "question",
        )
        kept = []
        for c in claims:
            src = str(getattr(c, "experiment_id", None) or "")
            if src in loop_keys:
                kept.append(c)
                continue
            aid = getattr(c, "attempt_id", None)
            if aid:
                att = (getattr(runtime, "attempts", None) or {}).get(str(aid))
                if isinstance(att, dict):
                    tid = str(att.get("task_id") or "")
                    task = (getattr(runtime, "tasks", None) or {}).get(tid)
                    if task is not None and task.spec.task_key in loop_keys:
                        kept.append(c)
        scoped[ref] = kept
    return scoped


def hypothesis_verdict_scoped(
    runtime: Any,
    loop: LoopSpec,
    hid: str,
    *,
    upto_iter: int,
) -> str:
    """Verdict for one scope hypothesis using only this loop's claims."""
    study: Study | None = getattr(runtime, "study", None)
    scoped = _claims_for_loop_scope(runtime, loop, upto_iter=upto_iter)
    claims = scoped.get(hid) or []
    any_exp = bool(loop_task_keys(runtime, loop.loop_id, upto_iter=upto_iter))
    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    conf_k = study.confirmations_needed if study is not None else 1
    supp_k = study.supported_needed if study is not None else 2
    v, _ = verdict_fn(
        claims,
        any_exp,
        conf_k,
        refutations_needed=k_ref,
        refutation_priority=priority,
        supported_needed=supp_k,
    )
    return str(v)


def question_disposition_scoped(
    runtime: Any,
    loop: LoopSpec,
    qid: str,
    *,
    upto_iter: int,
) -> str:
    """Disposition value for one scope question using loop-scoped claims."""
    study: Study | None = getattr(runtime, "study", None)
    scoped = _claims_for_loop_scope(runtime, loop, upto_iter=upto_iter)
    claims = scoped.get(qid) or []
    counting = [c for c in claims if c.counts()]
    if not counting:
        return Disposition.NOT_ATTEMPTED.value
    any_exp = bool(loop_task_keys(runtime, loop.loop_id, upto_iter=upto_iter))
    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    conf_k = study.confirmations_needed if study is not None else 1
    supp_k = study.supported_needed if study is not None else 2
    v, _ = verdict_fn(
        claims,
        any_exp,
        conf_k,
        refutations_needed=k_ref,
        refutation_priority=priority,
        supported_needed=supp_k,
    )
    return disposition_for_question(
        hypothesis_verdicts={},
        question_verdict=v,
        has_hypotheses=False,
    )


def scope_fingerprint(runtime: Any, loop: LoopSpec, *, upto_iter: int) -> str:
    """sha256(JCS(sorted (h,verdict)/(q,disposition) pairs)) for no_change."""
    pairs: list[list[str]] = []
    for hid in sorted(loop.scope_hypotheses):
        pairs.append(
            [f"h:{hid}", hypothesis_verdict_scoped(runtime, loop, hid, upto_iter=upto_iter)]
        )
    for qid in sorted(loop.scope_questions):
        pairs.append(
            [
                f"q:{qid}",
                question_disposition_scoped(runtime, loop, qid, upto_iter=upto_iter),
            ]
        )
    return sha256_hex(canonical_json(pairs))


def count_survivors(runtime: Any, loop: LoopSpec, *, upto_iter: int) -> int:
    """|{h ∈ scope_hypotheses : verdict(h) ∉ {refuted}}| under loop scope."""
    n = 0
    for hid in loop.scope_hypotheses:
        v = hypothesis_verdict_scoped(runtime, loop, hid, upto_iter=upto_iter)
        if v != "refuted":
            n += 1
    return n


def _pred_survivors_le(
    runtime: Any, loop: LoopSpec, *, iter_n: int, ctx: Mapping[str, Any]
) -> bool:
    n = _require_int_n(dict(loop.until.params or {}))
    return count_survivors(runtime, loop, upto_iter=iter_n) <= n


def _pred_criterion_met(
    runtime: Any, loop: LoopSpec, *, iter_n: int, ctx: Mapping[str, Any]
) -> bool:
    """True when any loop-body result (upto iter) has criterion_id MET."""
    cid = str(loop.until.params.get("criterion_id") or "")
    if not cid:
        return False
    loop_keys = loop_task_keys(runtime, loop.loop_id, upto_iter=iter_n)
    results = getattr(runtime, "results", None) or {}
    tasks = getattr(runtime, "tasks", None) or {}
    for _aid, row in results.items():
        if not isinstance(row, dict):
            continue
        att = (getattr(runtime, "attempts", None) or {}).get(_aid)
        tid = None
        if isinstance(att, dict):
            tid = att.get("task_id")
        if tid is None:
            tid = row.get("task_id")
        task = tasks.get(str(tid)) if tid else None
        if task is None or task.spec.task_key not in loop_keys:
            continue
        for cr in row.get("check_results") or []:
            if not isinstance(cr, dict):
                continue
            tid_c = str(cr.get("template_id") or cr.get("check_id") or "")
            if tid_c != cid:
                continue
            st = cr.get("status")
            sv = getattr(st, "value", st)
            if str(sv) == ReqStatus.MET.value:
                return True
    return False


def _pred_no_change(
    runtime: Any, loop: LoopSpec, *, iter_n: int, ctx: Mapping[str, Any]
) -> bool:
    if iter_n <= 1:
        return False
    fp_cur = scope_fingerprint(runtime, loop, upto_iter=iter_n)
    fp_prev = scope_fingerprint(runtime, loop, upto_iter=iter_n - 1)
    return fp_cur == fp_prev


# Built-ins (DESIGN_M1 §4) — registered once; overrides raise.
register_loop_predicate("survivors_le", _pred_survivors_le)
register_loop_predicate("criterion_met", _pred_criterion_met)
register_loop_predicate("no_change", _pred_no_change)
