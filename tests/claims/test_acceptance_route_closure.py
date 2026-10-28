"""Acceptance-route closure in dispositions (universal-acceptance S2, §8.i/§8.ii).

Routed questions (acceptance_route stamped on the accepted plan) close as
``answered`` on proven receipts when the claims engine has no counting claims.
Claims stay the primary verdict engine; bare existence never closes; legacy
plans without a route keep the pre-S2 behavior byte-identical (§8.v).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    OutputSpec,
    RequirementRef,
    RequirementTemplate,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.finalize.study_view import study_view

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _exists_rubric() -> Rubric:
    return freeze_rubric(
        Rubric(
            items=(
                RubricItem(
                    id="exists",
                    text="required artifact exists",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                ),
            ),
            aggregate="all_required",
            k=None,
        )
    )


def _substantive_template(tid: str = "hera_registered") -> RequirementTemplate:
    return RequirementTemplate(
        id=tid,
        title=tid,
        text_ru="Registered receipt carries the declared content.",
        version=1,
        required=True,
        role="prediction",
        method_sources=["t"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        exempt_from_freeze=True,
        check=CodeCheck(
            kind="json_pointer",
            output="main",
            pointer="/dataset/rows",
            allow_network=False,
            allow_cgroup_degraded=False,
        ),
    )


def _catalog(*templates: RequirementTemplate) -> Catalog:
    return Catalog(catalog_version=1, edition="route-closure-tests", templates=list(templates))


def _spec(
    *,
    criteria: list[RequirementRef] | None = None,
    output_name: str = "main",
) -> TaskSpec:
    return TaskSpec(
        task_key="E0",
        title="addressing",
        goal="g",
        route="experiment",
        addresses=["Q1"],
        expected_outputs=[
            OutputSpec(name=output_name, media_type="application/json", rubric=_exists_rubric())
        ],
        criteria=criteria or [],
    )


def _task(spec: TaskSpec) -> Task:
    return Task(
        task_id="s/E0",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.DONE,
        deadline=NOW + timedelta(hours=1),
        attempts=["a1"],
        outcome=TaskOutcome.SUCCESS,
    )


def _rubric_row(rub: Rubric, *, output: str, status: str = "MET") -> dict[str, Any]:
    return {
        "check_id": f"rubric:{rub.digest}#out",
        "template_id": f"rubric:{rub.digest}",
        "status": status,
        "required": True,
        "role": "validity",
        "effect": {"rubric_digest": rub.digest, "rubric_kind": "rubric", "output": output},
    }


def _criterion_row(tid: str, *, status: str = "MET") -> dict[str, Any]:
    return {
        "check_id": f"{tid}#0",
        "template_id": tid,
        "status": status,
        "required": True,
        # Prediction-role criterion: FAILED caps the verdict half without
        # tainting the rubric (precedent: test_prediction_refutation_*).
        "role": "prediction",
        "effect": {"est": 225, "kind": "point"} if status == "MET" else None,
    }


def _results(*check_rows: dict[str, Any], **extra: Any) -> dict[str, dict[str, Any]]:
    row: dict[str, Any] = {
        "attempt_id": "a1",
        "result_class": "scientific",
        "provenance": "real",
        "check_results": list(check_rows),
        "artifacts": [],
        "output_map": {},
    }
    row.update(extra)
    return {"a1": row}


def _plans(
    *,
    final_type: str = "both",
    acceptance_route: str | None = "catalog_verified",
    output_name: str = "main",
) -> dict[str, dict[str, Any]]:
    q: dict[str, Any] = {"id": "Q1", "text": "what was measured?", "final_type": final_type}
    if acceptance_route is not None:
        q["acceptance_route"] = acceptance_route
    return {
        "p1": {
            "questions": [q],
            "tasks": [_spec(criteria=None, output_name=output_name).model_dump(mode="json")],
        }
    }


def _disp(plans: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
    return dispositions(
        tasks={
            "s/E0": _task(
                _spec(criteria=kwargs.pop("criteria", None), output_name=kwargs.pop("output_name", "main"))
            )
        },
        plans=plans,
        graph=None,
        study=None,
        results=kwargs.pop("results", None) or _results(),
        artifact_lookup=kwargs.pop("artifact_lookup", None),
        **kwargs,
    )


def test_catalog_verified_receipts_answer_both_and_fulfill_study(tmp_path) -> None:
    """S2 headline: routed receipts close the no-counting-claims gap (live st_1a18 shape)."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered"))
    plans = _plans()
    qdisp = _disp(plans, criteria=[RequirementRef(template_id="hera_registered")], catalog=catalog, results=results)
    row = qdisp["Q1"]
    assert row["disposition"] == "answered"
    assert row["verdict"] == "answered"
    assert row["reason"] == "catalog_verified_receipts"
    assert row["final_type"] == "both"
    # Honest evidence: reason_detail cites the content-bearing check id.
    assert row["reason_detail"] == ["hera_registered#0"]
    # e2e-ish: study_fulfilled no longer caps the study at partial for ft=both.
    view = study_view(
        [_task(_spec(criteria=[RequirementRef(template_id="hera_registered")]))],
        finalized=True,
        attempts={"a1": {"closed": True}},
        question_dispositions=qdisp,
        question_final_types={"Q1": "both"},
    )
    assert view.verdict == "fulfilled"


def test_catalog_verified_failed_required_check_is_not_answered() -> None:
    """Required substantive check FAILED → existing not_attempted mapping, never answered."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered", status="FAILED"))
    qdisp = _disp(
        _plans(),
        criteria=[RequirementRef(template_id="hera_registered")],
        catalog=catalog,
        results=results,
    )
    row = qdisp["Q1"]
    assert row["disposition"] == "not_attempted"
    assert row["reason"] == "no_counting_claims"
    assert "verdict" not in row


def test_catalog_verified_presence_only_checks_cannot_answer() -> None:
    """artifact-present-only receipts deliver evidence but never close (design §8.i)."""
    rub = _exists_rubric()
    results = _results(_rubric_row(rub, output="main"))
    qdisp = _disp(_plans(), catalog=_catalog(_substantive_template()), results=results)
    row = qdisp["Q1"]
    assert row["disposition"] == "not_attempted"
    assert row["reason"] == "no_counting_claims"


def test_catalog_verified_without_catalog_is_fail_closed() -> None:
    """No catalog → the content bar is unverifiable → fail-closed not_attempted."""
    rub = _exists_rubric()
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered"))
    qdisp = _disp(
        _plans(),
        criteria=[RequirementRef(template_id="hera_registered")],
        results=results,
    )
    assert qdisp["Q1"]["disposition"] == "not_attempted"


def test_catalog_verified_presence_only_criterion_template_is_not_content() -> None:
    """A cited presence-only template (artifact_exists class) never satisfies the bar."""
    presence = RequirementTemplate(
        id="present",
        title="present",
        text_ru="Артефакт существует.",
        version=1,
        required=True,
        role="validity",
        method_sources=["t"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        exempt_from_freeze=True,
        check=CodeCheck(
            kind="artifact_exists",
            output="main",
            allow_network=False,
            allow_cgroup_degraded=False,
        ),
    )
    catalog = _catalog(presence, _substantive_template())
    results = _results(_criterion_row("present"))
    qdisp = _disp(
        _plans(final_type="verdict"),
        criteria=[RequirementRef(template_id="present")],
        catalog=catalog,
        results=results,
    )
    assert qdisp["Q1"]["disposition"] == "not_attempted"


def test_evidence_grounded_nonempty_receipt_answers(tmp_path) -> None:
    """grounded route: declared output with a successful non-empty receipt closes."""
    rub = _exists_rubric()
    store = LocalArtifactStore(tmp_path)
    art = store.put(b'{"rows": 12}', make_tool_ref("a1:coder"), name="coder", media_type="application/json")
    results = _results(
        _rubric_row(rub, output="coder"),
        artifacts=[art.artifact_id],
        output_map={"coder": art.artifact_id},
    )
    qdisp = _disp(
        _plans(final_type="verdict", acceptance_route="evidence_grounded", output_name="coder"),
        results=results,
        artifact_lookup=store.get_meta,
        output_name="coder",
    )
    row = qdisp["Q1"]
    assert row["disposition"] == "answered"
    assert row["reason"] == "grounded_receipt"
    assert row["verdict"] == "answered"
    assert row["reason_detail"] == [f"coder:{art.artifact_id}"]


def test_evidence_grounded_empty_or_missing_receipt_not_answered(tmp_path) -> None:
    rub = _exists_rubric()
    store = LocalArtifactStore(tmp_path)
    empty = store.put(b"", make_tool_ref("a1:coder"), name="coder", media_type="application/json")
    results_empty = _results(
        _rubric_row(rub, output="coder"),
        artifacts=[empty.artifact_id],
        output_map={"coder": empty.artifact_id},
    )
    row = _disp(
        _plans(final_type="verdict", acceptance_route="evidence_grounded", output_name="coder"),
        results=results_empty,
        artifact_lookup=store.get_meta,
        output_name="coder",
    )["Q1"]
    assert row["disposition"] == "not_attempted"

    # No receipt at all: bare MET artifact-existence rubric never closes.
    results_bare = _results(_rubric_row(rub, output="coder"))
    row = _disp(
        _plans(final_type="verdict", acceptance_route="evidence_grounded", output_name="coder"),
        results=results_bare,
        artifact_lookup=store.get_meta,
        output_name="coder",
    )["Q1"]
    assert row["disposition"] == "not_attempted"


def test_evidence_grounded_stub_provenance_receipt_not_answered(tmp_path) -> None:
    """A stub receipt is not evidence — honest fail-closed."""
    rub = _exists_rubric()
    store = LocalArtifactStore(tmp_path)
    art = store.put(b'{"rows": 12}', make_tool_ref("a1:coder"), name="coder", media_type="application/json")
    results = _results(
        _rubric_row(rub, output="coder"),
        artifacts=[art.artifact_id],
        output_map={"coder": art.artifact_id},
        provenance="stub",
    )
    row = _disp(
        _plans(final_type="verdict", acceptance_route="evidence_grounded", output_name="coder"),
        results=results,
        artifact_lookup=store.get_meta,
        output_name="coder",
    )["Q1"]
    assert row["disposition"] == "not_attempted"


def test_legacy_plan_without_route_unchanged() -> None:
    """Back-compat (§8.v): no route on the plan → pre-S2 rows byte-identical."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered"))
    criteria = [RequirementRef(template_id="hera_registered")]
    legacy = _disp(_plans(acceptance_route=None), criteria=criteria, catalog=catalog, results=results)
    assert legacy["Q1"] == {
        "disposition": "not_attempted",
        "reason": "no_counting_claims",
        "reason_detail": [],
        "formulation": "what was measured?",
        "final_type": "both",
    }
    # Same shape with a catalog present but no route recorded on the question.
    no_catalog = _disp(_plans(acceptance_route=None), criteria=criteria, results=results)
    assert no_catalog["Q1"] == legacy["Q1"]


def test_rubric_failed_beats_route_closure_for_both() -> None:
    """ft=both keeps its rubric requirement: rubric rejected ⇒ rubric_not_met wins."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(
        _rubric_row(rub, output="main", status="FAILED"),
        _criterion_row("hera_registered"),
    )
    qdisp = _disp(
        _plans(),
        criteria=[RequirementRef(template_id="hera_registered")],
        catalog=catalog,
        results=results,
    )
    row = qdisp["Q1"]
    assert row["disposition"] == "rubric_not_met"
    assert row["reason"] == "rubric_rejected"
    assert row["verdict"] == "rubric_not_met"


def test_hypothesis_route_does_not_close_on_receipts() -> None:
    """hypothesis_claimed closes via the claims engine only — never via receipts."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered"))
    qdisp = _disp(
        _plans(final_type="verdict", acceptance_route="hypothesis_claimed"),
        criteria=[RequirementRef(template_id="hera_registered")],
        catalog=catalog,
        results=results,
    )
    assert qdisp["Q1"]["disposition"] == "not_attempted"
    assert qdisp["Q1"]["reason"] == "no_counting_claims"


def test_infra_result_row_is_not_a_receipt() -> None:
    """Timeout/infra result classes are pending, never route receipts."""
    rub = _exists_rubric()
    catalog = _catalog(_substantive_template())
    results = _results(
        _rubric_row(rub, output="main"),
        _criterion_row("hera_registered"),
        result_class="infra",
    )
    qdisp = _disp(
        _plans(),
        criteria=[RequirementRef(template_id="hera_registered")],
        catalog=catalog,
        results=results,
    )
    assert qdisp["Q1"]["disposition"] == "not_attempted"


def test_unknown_route_value_keeps_legacy_behavior() -> None:
    """Garbage route values are skipped (same first-wins guard as final_type)."""
    rub = _exists_rubric()
    results = _results(_rubric_row(rub, output="main"), _criterion_row("hera_registered"))
    plans = _plans(acceptance_route="catalog_verified")
    plans["p1"]["questions"][0]["acceptance_route"] = "operator_said_so"
    qdisp = _disp(plans, results=results)
    assert qdisp["Q1"]["disposition"] == "not_attempted"
