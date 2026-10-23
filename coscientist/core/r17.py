"""R17 (H-e): predeclared criterion vs evidence; threshold source must not count."""

from __future__ import annotations


def r17_criterion_before_evidence(
    criterion_frozen_at_seq: int, first_cited_evidence_seq: int
) -> bool:
    """True when criterion was frozen before the first evidence the claim cites.

    DESIGN_M1 §12: ``frozen_at_seq < seq`` of the first evidence referenced by a
    claim with ``checked_against`` = this criterion (not any evidence in the study).
    """
    return criterion_frozen_at_seq < first_cited_evidence_seq


def r17_threshold_source_not_counted(
    confirming_evidence_ids: set[str],
    threshold_source_evidence_ids: set[str],
) -> set[str]:
    """Drop evidence that chose the threshold from the confirming set.

    Evidence used to pick a criterion threshold (e.g. a number from a paper)
    must not also count as confirmation of that same criterion.
    """
    return set(confirming_evidence_ids) - set(threshold_source_evidence_ids)


def claim_counts_under_r17(
    *,
    criterion_frozen_at_seq: int | None,
    first_cited_evidence_seq: int | None,
    confirming_evidence_ids: set[str],
    threshold_source_evidence_ids: set[str],
) -> bool:
    """Whether a claim may count toward verdict under R17 constraints."""
    if criterion_frozen_at_seq is None or first_cited_evidence_seq is None:
        return False
    if not r17_criterion_before_evidence(
        criterion_frozen_at_seq, first_cited_evidence_seq
    ):
        return False
    usable = r17_threshold_source_not_counted(
        confirming_evidence_ids, threshold_source_evidence_ids
    )
    return bool(usable)
