"""Derivation evidence kind (M1 PR15 / DESIGN_M1 §6.6).

``evidence_kind=derivation`` counts toward verdict:
- review-only (ReviewCheck / rubric judge, no executable CodeCheck) → ceiling
  ``inconclusive``; verdict marked ``by_theory``;
- with executable CodeCheck MET → full counting; still ``by_theory``.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_KIND_DERIVATION = "derivation"
# Rubric item ids that mark a first-principles derivation deliverable (§6.6).
DERIVATION_RUBRIC_IDS: frozenset[str] = frozenset({"steps", "assumptions"})
VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "computational",
        "experimental",
        "literature",
        "expert",
        "derivation",
    }
)

# Executable CodeCheck kinds that lift the review-only ceiling (§7 / PR17).
_EXECUTABLE_KINDS: frozenset[str] = frozenset(
    {
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
        "metric_threshold",
        "value_matches",
        "json_pointer",
        "artifact_exists",
        "schema_valid",
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
    """True when counted claims are derivation-only without executable checks."""
    counted = [c for c in claims if _counts(c)]
    if not counted:
        return False
    if not all(is_derivation(c) for c in counted):
        return False
    return not any(derivation_executable(c) for c in counted)


def by_theory_mark(claims: list[Any]) -> bool:
    """Mark verdict «теоретически» when ≥1 counting derivation claim contributed."""
    return any(is_derivation(c) and _counts(c) for c in claims)


def apply_derivation_ceiling(verdict_value: str, claims: list[Any]) -> str:
    """Cap supported/survived/refuted → inconclusive for review-only derivation."""
    if verdict_value not in ("supported", "survived", "refuted"):
        return verdict_value
    if derivation_caps_inconclusive(claims):
        return "inconclusive"
    return verdict_value


def _counts(claim: Any) -> bool:
    fn = getattr(claim, "counts", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return False


def is_derivation_task_spec(spec: Any | None) -> bool:
    """True when OutputSpec role=code|report carries steps+assumptions rubric."""
    if spec is None:
        return False
    from coscientist.core.rubric_types import Rubric

    for o in getattr(spec, "expected_outputs", None) or []:
        role = getattr(o, "role", None)
        if role not in ("code", "report"):
            continue
        rub = getattr(o, "rubric", None)
        if rub is None:
            continue
        try:
            r = rub if isinstance(rub, Rubric) else Rubric.model_validate(rub)
        except Exception:  # noqa: BLE001
            continue
        ids = {it.id for it in r.items if it.required}
        if DERIVATION_RUBRIC_IDS <= ids:
            return True
    return False


def executable_check_met(checks: list[Any] | None) -> bool:
    """True if any non-rubric executable CodeCheck is MET/PASSED."""
    from coscientist.core.domain import ReqStatus

    for c in checks or ():
        kind = getattr(c, "kind", None)
        if kind is None:
            # ReviewCheck — no kind
            continue
        kind_s = getattr(kind, "value", kind)
        if str(kind_s) == "rubric":
            continue
        if str(kind_s) not in _EXECUTABLE_KINDS:
            continue
        st = getattr(c, "status", None)
        st_s = getattr(st, "value", st)
        if st_s in (ReqStatus.MET, ReqStatus.WAIVED, "MET", "PASSED", "WAIVED"):
            return True
    return False
