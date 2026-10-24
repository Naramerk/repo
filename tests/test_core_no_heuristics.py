"""Missing / unknown domain values must raise (no silent defaults)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.claims.claims import RuntimeClaim
from coscientist.core.claims.verdict import recompute_effect_provenance
from coscientist.core.events.errors import (
    ArtifactRejected,
    JournalFormatError,
    SandboxInfraError,
    require_journal_field,
)
from coscientist.core.execution.artifacts import (
    LocalArtifactStore,
    make_tool_ref,
    output_kind_from_spec,
)
from coscientist.core.execution.domain import HitlPolicy, InputSpec, OutputSpec, QuestionSpec, TaskKind
from coscientist.core.execution.executor import (
    _check_role,
    _criterion_key_for_claim,
    _criterion_status_for_claim,
)
from coscientist.core.plan.foreach import _nat_key


def test_output_spec_role_required():
    with pytest.raises(ValidationError):
        OutputSpec(name="main", media_type="application/json")


def test_question_spec_final_type_required():
    with pytest.raises(ValidationError):
        QuestionSpec(id="Q1", text="q")


def test_input_spec_quorum_rejects_zero():
    with pytest.raises(ValidationError, match="quorum"):
        InputSpec(from_task="E0", output="main", quorum=0)


def test_hitl_gate_unknown_kind_raises():
    with pytest.raises(KeyError, match="unknown HITL gate"):
        HitlPolicy().gate("not_a_gate")


def test_checked_against_required():
    with pytest.raises(ValueError, match="checked_against"):
        _criterion_key_for_claim({}, checks=[], criteria=[])


def test_unknown_check_role_raises():
    with pytest.raises(ValueError, match="unknown check role"):
        _check_role({"role": "weird"})


def test_missing_check_status_for_criterion_raises():
    with pytest.raises(ValueError, match="no check status"):
        _criterion_status_for_claim(
            {"checked_against": "missing_tmpl"},
            checks=[{"template_id": "other", "status": "MET", "role": "prediction"}],
            criteria=[],
        )


def test_effect_missing_producer_raises():
    with pytest.raises(ValueError, match="producer"):
        recompute_effect_provenance({"est": 1.0, "kind": "point", "core_evaluated": True})


def test_output_kind_from_spec_requires_role():
    assert output_kind_from_spec(role="code") == "code"
    assert output_kind_from_spec(role="data") == "data"
    with pytest.raises(TypeError):
        output_kind_from_spec()  # type: ignore[call-arg]


def test_task_kind_from_route_rejects_alias():
    """foreach uses TaskKind(route); unknown route raises (no lit/hyp aliases)."""
    with pytest.raises(ValueError):
        TaskKind("lit")
    with pytest.raises(ValueError):
        TaskKind("hyp")


def test_artifact_put_requires_role(tmp_path):
    store = LocalArtifactStore(tmp_path)
    with pytest.raises((TypeError, ArtifactRejected)):
        store.put(b"x", make_tool_ref("c1"), name="main", media_type="text/plain")  # type: ignore[call-arg]


def test_nat_key_no_regex():
    assert _nat_key("H2") < _nat_key("H10")


def test_missing_charged_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="charged") as ei:
        require_journal_field({"closed": True, "provenance": "real"}, "charged", ctx="a1")
    assert ei.value.field == "charged"


def test_missing_provenance_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="provenance") as ei:
        require_journal_field({"closed": True, "charged": False}, "provenance", ctx="a1")
    assert ei.value.field == "provenance"


def test_missing_attempt_role_raises_journal_format_error():
    with pytest.raises(JournalFormatError, match="attempt_role") as ei:
        require_journal_field(
            {"provenance": "real", "charged": False, "closed": False},
            "attempt_role",
            ctx="a1",
        )
    assert ei.value.field == "attempt_role"


def test_missing_target_kind_raises_type_error():
    with pytest.raises(TypeError):
        RuntimeClaim(criterion_status="met", check_role="prediction")  # type: ignore[call-arg]


def test_sandbox_infra_requires_transient():
    with pytest.raises(TypeError):
        SandboxInfraError("oom")  # type: ignore[call-arg]
