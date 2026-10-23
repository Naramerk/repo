"""Derivation evidence kind (M1 PR15 / DESIGN_M1 §6.6).

``evidence_kind=derivation`` is set only via explicit ``OutputSpec.evidence_kind``
(PlanValidator-enforced). Rubric item ids never imply derivation.

- review-only (ReviewCheck / rubric judge, no real executable CodeCheck) →
  **never counts** toward k_sup/k_ref; may still mark ``by_theory`` and yield
  ``inconclusive`` when it is the only evidence;
- with a real executable CodeCheck MET (not WAIVED) bound to the claim →
  full counting + ``by_theory``.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_KIND_DERIVATION = "derivation"
VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "computational",
        "experimental",
        "literature",
        "expert",
        "derivation",
    }
)

# Real executable CodeCheck kinds that lift the review-only ceiling (§7 / PR17).
# Presence/schema probes (artifact_exists, schema_valid, json_pointer) do NOT count.
_EXECUTABLE_KINDS: frozenset[str] = frozenset(
    {
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
        "metric_threshold",
        "value_matches",
    }
)

__all__ = [
    "EVIDENCE_KIND_DERIVATION",
    "VALID_EVIDENCE_KINDS",
    "is_derivation",
    "derivation_executable",
    "derivation_caps_inconclusive",
    "by_theory_mark",
    "apply_derivation_ceiling",
    "is_derivation_task_spec",
    "executable_check_met",
]


def is_derivation(claim: Any) -> bool:
    if getattr(claim, "is_derivation", None) is True:
        return True
    return getattr(claim, "evidence_kind", "") == EVIDENCE_KIND_DERIVATION


def derivation_executable(claim: Any) -> bool:
    """True when core stamped ``derivation_executable`` (executable check MET)."""
    flag = getattr(claim, "derivation_executable", None)
    if flag is not None:
        return bool(flag)
    effect = getattr(claim, "effect", None)
    if isinstance(effect, dict) and effect.get("derivation_executable") is True:
        return True
    return False


def derivation_caps_inconclusive(claims: list[Any]) -> bool:
    """True when only review-only derivation is present (nothing else counts).

    Review-only derivation never enters ``counts()``. Mixed with counting
    computational/experimental claims must **not** cap those claims' verdict.
    """
    ders = [c for c in claims if is_derivation(c)]
    if not ders:
        return False
    if any(derivation_executable(c) for c in ders):
        return False
    for c in claims:
        if is_derivation(c):
            continue
        fn = getattr(c, "counts", None)
        if callable(fn):
            try:
                if bool(fn()):
                    return False
            except (TypeError, ValueError, AttributeError):
                continue
    return True


def by_theory_mark(claims: list[Any]) -> bool:
    """Mark verdict «теоретически» when ≥1 derivation claim is present."""
    return any(is_derivation(c) for c in claims)


def apply_derivation_ceiling(verdict_value: str, claims: list[Any]) -> str:
    """Cap supported/survived/refuted → inconclusive for review-only derivation."""
    if verdict_value not in ("supported", "survived", "refuted"):
        return verdict_value
    if derivation_caps_inconclusive(claims):
        return "inconclusive"
    return verdict_value


def is_derivation_task_spec(spec: Any | None) -> bool:
    """True when any OutputSpec declares ``evidence_kind=derivation`` (explicit only)."""
    if spec is None:
        return False
    for o in getattr(spec, "expected_outputs", None) or []:
        if getattr(o, "evidence_kind", None) == EVIDENCE_KIND_DERIVATION:
            return True
    return False


def executable_check_met(
    checks: list[Any] | None,
    *,
    bound_key: str | None = None,
) -> bool:
    """True if a real executable CodeCheck bound to the claim is MET/PASSED.

    When ``bound_key`` is set, only checks whose ``template_id`` or ``check_id``
    equals that key are considered — unrelated MET checks must not lift §6.6.
    When ``bound_key`` is None/empty, return False (fail-closed).
    """
    from coscientist.core.execution.domain import ReqStatus

    if not bound_key:
        return False
    key = str(bound_key)
    for c in checks or ():
        tid = getattr(c, "template_id", None)
        cid = getattr(c, "check_id", None)
        if tid is None and isinstance(c, dict):
            tid = c.get("template_id")
            cid = c.get("check_id")
        if str(tid or "") != key and str(cid or "") != key:
            continue
        kind = getattr(c, "kind", None)
        if kind is None and isinstance(c, dict):
            kind = c.get("kind")
        if kind is None:
            continue  # ReviewCheck — no kind
        kind_s = getattr(kind, "value", kind)
        if str(kind_s) == "rubric":
            continue
        if str(kind_s) not in _EXECUTABLE_KINDS:
            continue
        st = getattr(c, "status", None)
        if st is None and isinstance(c, dict):
            st = c.get("status")
        st_s = getattr(st, "value", st)
        if st_s in (ReqStatus.MET, "MET", "PASSED"):
            return True
    return False
