"""Intake stage ``TaskSpec.required`` + SPEC output name from catalog."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import CodeCheck, ReviewCheckPlain, TaskKind
from coscientist.core.plan.lifecycle import _artifact_present_output_name
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime, open_runtime


def _artifact_present_output(catalog) -> str:
    tmpl = next(t for t in catalog.templates if t.id == "artifact_present")
    assert isinstance(tmpl.check, CodeCheck)
    assert type(tmpl.check.output) is str
    return tmpl.check.output


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda c: c.model_copy(
                update={"templates": [t for t in c.templates if t.id != "artifact_present"]}
            ),
            "catalog_missing_template:artifact_present",
        ),
        (
            lambda c: c.model_copy(
                update={
                    "templates": [
                        (
                            t.model_copy(
                                update={
                                    "check": ReviewCheckPlain(
                                        measured=False,
                                        reviewer_role="reviewer",
                                        question="q",
                                        max_calls=1,
                                    )
                                }
                            )
                            if t.id == "artifact_present"
                            else t
                        )
                        for t in c.templates
                    ]
                }
            ),
            "check_must_be_CodeCheck",
        ),
        (
            lambda c: c.model_copy(
                update={
                    "templates": [
                        (
                            t.model_copy(
                                update={
                                    "check": t.check.model_copy(
                                        update={"kind": "schema_valid", "output": "main"}
                                    )
                                }
                            )
                            if t.id == "artifact_present"
                            else t
                        )
                        for t in c.templates
                    ]
                }
            ),
            "kind_must_be_artifact_exists",
        ),
        (
            lambda c: c.model_copy(
                update={
                    "templates": [
                        (
                            t.model_copy(
                                update={"check": t.check.model_copy(update={"output": ""})}
                            )
                            if t.id == "artifact_present"
                            else t
                        )
                        for t in c.templates
                    ]
                }
            ),
            "output_must_be_non_empty_str",
        ),
    ],
    ids=["missing_template", "not_code_check", "wrong_kind", "empty_output"],
)
def test_artifact_present_output_name_refuses(mutate, match: str) -> None:
    catalog = mutate(minimal_test_catalog())
    with pytest.raises(CatalogError, match=match):
        _artifact_present_output_name(catalog)


@pytest.mark.asyncio
async def test_intake_hyp_lit_report_not_required(tmp_path, clock) -> None:
    """HYP/LIT/REPORT required=False so Need-off cancel/SKIPPED can still fulfill."""
    sid = "st_stage_required"
    catalog = minimal_test_catalog()
    rt = await open_runtime(tmp_path, sid, catalog=catalog, clock=clock, owner="t1")
    try:
        await rt.create_study(
            profile="research",
            request="stage required flags",
            catalog_version=catalog.catalog_version,
            deadline=clock.now() + timedelta(seconds=600),
            sid=sid,
            finalize_reserve_s=30,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            inputs=[],
        )
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert by_key["SPEC"].spec.required is True
        assert by_key["SPEC"].kind == TaskKind.SPEC
        # Single source: catalog artifact_present.check.output (not a lifecycle literal).
        assert [o.name for o in by_key["SPEC"].spec.expected_outputs] == [
            _artifact_present_output(catalog)
        ]
        assert by_key["HYP"].spec.required is False
        assert by_key["HYP"].kind == TaskKind.HYPOTHESES
        assert by_key["LIT"].spec.required is False
        assert by_key["LIT"].kind == TaskKind.LITERATURE
        assert by_key["PLAN"].spec.required is True
        assert by_key["PLAN"].spec.expected_outputs == []
        assert by_key["REPORT"].spec.required is False
        assert by_key["REPORT"].kind == TaskKind.REPORT
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_spec_expected_output_follows_catalog_artifact_present_output(
    tmp_path, clock
) -> None:
    """Changing catalog check.output changes SPEC expected_outputs (one source)."""
    base = minimal_test_catalog()
    rewritten = []
    for tmpl in base.templates:
        if tmpl.id != "artifact_present":
            rewritten.append(tmpl)
            continue
        assert isinstance(tmpl.check, CodeCheck)
        rewritten.append(
            tmpl.model_copy(
                update={"check": tmpl.check.model_copy(update={"output": "spec_blob"})}
            )
        )
    catalog = base.model_copy(update={"templates": rewritten})
    assert _artifact_present_output(catalog) == "spec_blob"
    sid = "st_spec_from_catalog"
    rt = await open_runtime(tmp_path, sid, catalog=catalog, clock=clock, owner="t1")
    try:
        await rt.create_study(
            profile="research",
            request="catalog-driven SPEC output",
            catalog_version=catalog.catalog_version,
            deadline=clock.now() + timedelta(seconds=600),
            sid=sid,
            finalize_reserve_s=30,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            inputs=[],
        )
        spec = next(t for t in rt.tasks.values() if t.spec.task_key == "SPEC")
        assert [o.name for o in spec.spec.expected_outputs] == ["spec_blob"]
    finally:
        await close_runtime(rt)
