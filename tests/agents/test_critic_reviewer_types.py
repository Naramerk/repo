"""CriticVerdict / ReviewVerdict typed models — DESIGN §7.1 (no runner)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.agents.critic import CRITIC_PROMPT_REF, CriticIssue, CriticVerdict
from coscientist.agents.prompts.builtin import (
    CRITIC_PROMPT_TEMPLATE,
    REVIEWER_PROMPT_TEMPLATE,
    builtin_role_prompt_refs,
    builtin_role_prompt_registry,
)
from coscientist.agents.reviewer import (
    REVIEWER_PROMPT_REF,
    ReviewEvidenceRef,
    ReviewVerdict,
)


def _assert_err(exc: ValidationError, *, loc: tuple[Any, ...], err_type: str) -> None:
    (err,) = exc.errors()
    assert err["loc"] == loc
    assert err["type"] == err_type


def _issue(**over: object) -> CriticIssue:
    base: dict[str, object] = {"path": "EXP-0", "message": "missing criterion"}
    base.update(over)
    return CriticIssue.model_validate(base)


def _critic(**over: object) -> CriticVerdict:
    base: dict[str, object] = {"decision": "accept", "issues": ()}
    base.update(over)
    return CriticVerdict.model_validate(base)


def _eref(**over: object) -> ReviewEvidenceRef:
    base: dict[str, object] = {
        "artifact_id": "art_1",
        "digest": "a" * 64,
        "json_pointer": "/metric",
    }
    base.update(over)
    return ReviewEvidenceRef.model_validate(base)


def _review(**over: object) -> ReviewVerdict:
    base: dict[str, object] = {
        "decision": "pass",
        "reasons": ("matches prediction",),
        "evidence": (_eref(),),
        "reviewer": "reviewer",
        "model": "test-model",
    }
    base.update(over)
    return ReviewVerdict.model_validate(base)


def test_critic_accept_empty_issues() -> None:
    v = _critic()
    assert v.decision == "accept"
    assert v.issues == ()


def test_critic_revise_requires_issues() -> None:
    with pytest.raises(ValidationError, match="non-empty issues"):
        _critic(decision="revise", issues=())


def test_critic_reject_with_issues_round_trip() -> None:
    v = _critic(decision="reject", issues=(_issue(),))
    assert v.model_validate_json(v.model_dump_json()) == v


def test_critic_rejects_blank_issue_message() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch") as ei:
        CriticIssue.model_validate({"path": "PLAN-0", "message": "   "})
    _assert_err(ei.value, loc=("message",), err_type="string_pattern_mismatch")


def test_critic_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError, match="literal_error") as ei:
        CriticVerdict.model_validate({"decision": "maybe", "issues": ()})
    _assert_err(ei.value, loc=("decision",), err_type="literal_error")


def test_critic_frozen() -> None:
    v = _critic()
    with pytest.raises(ValidationError, match="frozen"):
        v.__setattr__("decision", "reject")


def test_review_pass_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="non-empty evidence"):
        _review(evidence=())


def test_review_fail_allows_empty_evidence() -> None:
    v = _review(decision="fail", evidence=(), reasons=("schema invalid",))
    assert v.decision == "fail"
    assert v.evidence == ()


def test_review_round_trip_json() -> None:
    v = _review(evidence=(_eref(json_pointer=""),))
    assert v.model_validate_json(v.model_dump_json()) == v
    assert v.evidence[0].json_pointer == ""


def test_review_evidence_requires_json_pointer_key() -> None:
    with pytest.raises(ValidationError, match="json_pointer"):
        ReviewEvidenceRef.model_validate(
            {"artifact_id": "art_1", "digest": "a" * 64}
        )


def test_review_rejects_blank_reason_via_field_pattern() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        _review(reasons=("ok", "  "))


def test_review_rejects_extra_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden") as ei:
        ReviewVerdict.model_validate(
            {
                "decision": "unassessed",
                "reasons": (),
                "evidence": (),
                "reviewer": "reviewer",
                "model": "m",
                "extra": 1,
            }
        )
    _assert_err(ei.value, loc=("extra",), err_type="extra_forbidden")


def test_builtin_prompts_cover_critic_and_reviewer() -> None:
    refs = builtin_role_prompt_refs()
    assert refs == frozenset({CRITIC_PROMPT_REF, REVIEWER_PROMPT_REF})
    reg = builtin_role_prompt_registry()
    assert reg.get(CRITIC_PROMPT_REF) == CRITIC_PROMPT_TEMPLATE
    assert reg.get(REVIEWER_PROMPT_REF) == REVIEWER_PROMPT_TEMPLATE
    critic_text = reg.render(
        CRITIC_PROMPT_REF,
        {"request": "measure X", "plan_json": "{}"},
    )
    assert "CriticVerdict" in critic_text
    assert "measure X" in critic_text
    review_text = reg.render(
        REVIEWER_PROMPT_REF,
        {"question": "Is MET?", "evidence_json": "[]"},
    )
    assert "ReviewVerdict" in review_text
    assert "Is MET?" in review_text
