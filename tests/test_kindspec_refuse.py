"""Fail-closed KindSpec dumps: no invent, typed refuse when required attrs missing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coscientist.core.events.payload_schemas import GraphNodeUpsertedPayload
from coscientist.core.execution.domain import (
    InputBinding,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.executor import _derive_claim_core_attrs
from coscientist.core.execution.inputs import InputUnavailable, uses_claim_graph_events
from coscientist.core.plan.plan_validate import PLAN_REFUSED_LINE
from tests.helpers import NEED_OFF, close_runtime, open_runtime


def test_criterion_missing_metric_op_target_refused() -> None:
    with pytest.raises(
        ValidationError,
        match=r"criterion missing required attrs: .*metric",
    ):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "criterion",
                "study_id": "s",
                "attrs": {"check": "code", "frozen": True, "template_id": "x"},
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_criterion_rubric_digest_waives_metric_op_target() -> None:
    """PR13: check=rubric + rubric_digest — no metric/op/target invent (DESIGN_M1 §6.2)."""
    GraphNodeUpsertedPayload.model_validate(
        {
            "node_id": "n1",
            "kind": "criterion",
            "study_id": "s",
            "attrs": {
                "check": "rubric",
                "rubric_digest": "a" * 64,
                "frozen": True,
                "affects_stance": False,
            },
            "ref_id": None,
            "parent_id": None,
            "created_at": None,
        }
    )


def test_experiment_missing_attempts_refused() -> None:
    with pytest.raises(
        ValidationError,
        match=r"experiment missing required attrs: \['attempts'\]",
    ):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "experiment",
                "study_id": "s",
                "attrs": {
                    "task_key": "EXP-1",
                    "title": "e",
                    "goal": "e",
                    "route": "experiment",
                    "required": True,
                },
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_uses_claim_refuses_experiment_without_attempts() -> None:
    task = Task(
        task_id="s/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment"),
        state=TaskState.PENDING,
        deadline=datetime.now(timezone.utc),
        attempts=[],
    )
    with pytest.raises(InputUnavailable, match="experiment_attempts_required"):
        uses_claim_graph_events(
            study_id="s",
            task=task,
            bindings=[
                InputBinding(
                    input="c",
                    kind="claim",
                    ref="s:claim:c1",
                    source="real",
                ),
            ],
            now=datetime.now(timezone.utc),
            graph=None,
            current_attempt_id=None,
        )


def test_uses_claim_includes_current_attempt_id() -> None:
    task = Task(
        task_id="s/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment"),
        state=TaskState.PENDING,
        deadline=datetime.now(timezone.utc),
        attempts=[],
    )
    evs = uses_claim_graph_events(
        study_id="s",
        task=task,
        bindings=[
            InputBinding(
                input="c",
                kind="claim",
                ref="s:claim:c1",
                source="real",
            ),
        ],
        now=datetime.now(timezone.utc),
        graph=None,
        current_attempt_id="s/EXP-1/1",
    )
    upsert = next(e for e in evs if e.type == "GraphNodeUpserted")
    assert upsert.payload["attrs"]["attempts"] == [{"attempt_id": "s/EXP-1/1"}]


def test_claim_without_checked_against_is_dropped() -> None:
    """Agent claim missing checked_against → drop (no invent from sole criterion)."""
    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    g = GraphStore(default_execution_registry())
    now = datetime.now(timezone.utc)
    g.upsert_node(
        GraphNode(
            node_id="s:question:Q1",
            kind="question",
            study_id="s",
            ref_id="Q1",
            attrs={"formulation": "q"},
            created_at=now,
        )
    )
    task = SimpleNamespace(
        task_id="s/EXP-1",
        spec=TaskSpec(
            task_key="EXP-1",
            title="e",
            goal="e",
            route="experiment",
            criteria=[RequirementRef(template_id="value_match")],
        ),
    )
    events, drops = claim_nodes_from_attempt(
        study_id="s",
        attempt_id="s/EXP-1/1",
        claims=[{"id": "c1", "target": "Q1"}],
        now=now,
        attempt_outcome="SUCCESS",
        provenance="real",
        graph=g,
        derive_attrs=lambda raw, validated: _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw=raw,
            checks=[],
            graph=g,
            study_id="s",
            validated=validated,
        ),
    )
    assert events == []
    assert drops == [{"key": "c1", "reason": "checked_against_missing"}]


def test_derive_claim_core_does_not_invent_checked_against() -> None:
    task = SimpleNamespace(
        spec=TaskSpec(
            task_key="EXP-1",
            title="e",
            goal="e",
            route="experiment",
            criteria=[RequirementRef(template_id="value_match")],
        )
    )
    attrs = _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"target": "Q1", "id": "c1"},
        checks=[],
        graph=None,
        study_id="s",
        validated=True,
    )
    assert "checked_against" not in attrs


@pytest.mark.asyncio
async def test_accept_plan_refuses_criterion_freeze_incomplete(tmp_path) -> None:
    """Criteria whose catalog check lacks metric/op/target → PlanRefused."""
    from coscientist.checks.catalog import (
        CATALOG_VERSION,
        TEMPLATE_VERSION,
        load_catalog,
    )
    from coscientist.core.claims.graph import default_execution_registry

    catalog = load_catalog(
        {
            "catalog_version": CATALOG_VERSION,
            "edition": "incomplete-criterion",
            "sources_sha256": {"test": "abc"},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "reviewed-rules",
                    "excerpt": "x",
                    "digest": "abc",
                }
            ],
            "templates": [
                {
                    "id": "bare_artifact",
                    "title": "bare",
                    "text_ru": "no metric",
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "artifact_exists",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    "role": "validity",
                    "required": True,
                    "version": TEMPLATE_VERSION,
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                    "exempt_from_freeze": False,
                }
            ],
            "defaults": {"experiment": []},
            "code_output_names": ["code"],
        },
        kind_registry=default_execution_registry(),
    )
    rt = await open_runtime(tmp_path, "crit-inc", catalog=catalog)
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=CATALOG_VERSION,
            require_hypotheses=False,
        )
        pid = await rt.accept_plan(
            [
                TaskSpec(
                    task_key="EXP-1",
                    title="e",
                    goal="e",
                    route="experiment",
                    criteria=[RequirementRef(template_id="bare_artifact")],
                )
            ],
            questions=[],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
        )
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert "criterion_freeze_incomplete" in refused[0].payload["detail"]
        assert refused[0].payload.get("report_line") == PLAN_REFUSED_LINE
    finally:
        await close_runtime(rt)


def test_rubric_freeze_aggregate_required_raises() -> None:
    """Typed Rubric with digest but empty aggregate → CatalogError."""
    from coscientist.core.claims.rubric_types import Rubric, RubricItem
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import OutputSpec
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    dig = "b" * 64
    rub = Rubric.model_construct(
        items=[RubricItem(id="a", text="A", required=True, check="judge")],
        aggregate="",
        digest=dig,
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        expected_outputs=[OutputSpec(name="main", media_type="text/plain", rubric=rub)],
    )
    with pytest.raises(CatalogError, match=r"rubric_freeze_aggregate_required:" + dig):
        _plan_accept_freeze_rows(
            sid="s",
            tasks=[spec],
            catalog=None,
            graph=None,
        )


def test_criterion_freeze_check_required_raises() -> None:
    """Unknown template_id (no catalog row) → criterion_freeze_check_required."""
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="no_such_template")],
    )
    with pytest.raises(CatalogError, match=r"criterion_freeze_check_required:no_such_template"):
        _plan_accept_freeze_rows(
            sid="s",
            tasks=[spec],
            catalog=None,
            graph=None,
        )


def test_criterion_freeze_unknown_check_type_raises() -> None:
    """Template.check neither CodeCheck nor ReviewCheck* → unknown_check_type."""
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import Catalog, RequirementTemplate
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    tmpl = RequirementTemplate.model_construct(
        id="weird_check",
        title="w",
        text_ru="w",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        check=object(),
        role="validity",
        required=True,
        version=1,
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
        exempt_from_freeze=False,
    )
    catalog = Catalog.model_construct(
        catalog_version=4,
        edition="t",
        sources_sha256={"t": "abc"},
        rules=[],
        templates=[tmpl],
        defaults={},
        code_output_names=[],
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="weird_check")],
    )
    with pytest.raises(CatalogError, match=r"criterion_freeze_unknown_check_type:weird_check"):
        _plan_accept_freeze_rows(
            sid="s",
            tasks=[spec],
            catalog=catalog,
            graph=None,
        )


def test_criterion_rubric_without_digest_refused() -> None:
    """check=rubric + affects_stance=false without rubric_digest → refuse."""
    with pytest.raises(
        ValidationError,
        match=r"criterion missing required attrs: .*rubric_digest",
    ):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "criterion",
                "study_id": "s",
                "attrs": {
                    "check": "rubric",
                    "affects_stance": False,
                    "frozen": True,
                },
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_criterion_rubric_affects_stance_true_without_metric_refused() -> None:
    """check=rubric + affects_stance=true without metric → refuse (DESIGN_M1 §6.2)."""
    with pytest.raises(
        ValidationError,
        match=r"criterion missing required attrs: .*metric",
    ):
        GraphNodeUpsertedPayload.model_validate(
            {
                "node_id": "n1",
                "kind": "criterion",
                "study_id": "s",
                "attrs": {
                    "check": "rubric",
                    "affects_stance": True,
                    "rubric_digest": "a" * 64,
                    "frozen": True,
                },
                "ref_id": None,
                "parent_id": None,
                "created_at": None,
            }
        )


def test_graph_writer_rubric_without_digest_refused() -> None:
    """Same rubric-without-digest refuse on graph_writer path."""
    from coscientist.core.claims.graph import GraphNode, registry_from_schema
    from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted

    reg = registry_from_schema()
    now = datetime.now(timezone.utc)
    node = GraphNode(
        node_id="s:criterion:bad",
        kind="criterion",
        study_id="s",
        attrs={"check": "rubric", "affects_stance": False, "frozen": True},
        created_at=now,
    )
    with pytest.raises(
        GraphWriterError,
        match=r"criterion missing required attrs: .*rubric_digest",
    ):
        graph_node_upserted(node=node, ts=now, registry=reg, idem_extra=())


def test_graph_writer_rubric_stance_true_without_metric_refused() -> None:
    from coscientist.core.claims.graph import GraphNode, registry_from_schema
    from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted

    reg = registry_from_schema()
    now = datetime.now(timezone.utc)
    node = GraphNode(
        node_id="s:criterion:bad2",
        kind="criterion",
        study_id="s",
        attrs={
            "check": "rubric",
            "affects_stance": True,
            "rubric_digest": "a" * 64,
            "frozen": True,
        },
        created_at=now,
    )
    with pytest.raises(
        GraphWriterError,
        match=r"criterion missing required attrs: .*metric",
    ):
        graph_node_upserted(node=node, ts=now, registry=reg, idem_extra=())


def test_rubric_freeze_digest_required_raises() -> None:
    from coscientist.core.claims.rubric_types import Rubric, RubricItem
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import OutputSpec
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    rub = Rubric.model_construct(
        items=[RubricItem(id="a", text="A", required=True, check="judge")],
        aggregate="all",
        digest="",
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        expected_outputs=[OutputSpec(name="main", media_type="text/plain", rubric=rub)],
    )
    with pytest.raises(CatalogError, match=r"rubric_freeze_digest_required:output='main'"):
        _plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=None, graph=None)


def test_criterion_freeze_catalog_templates_required_raises() -> None:
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import Catalog
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    catalog = Catalog.model_construct(
        catalog_version=4,
        edition="t",
        sources_sha256={"t": "abc"},
        rules=[],
        templates="not-a-list",
        defaults={},
        code_output_names=[],
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="x")],
    )
    with pytest.raises(
        CatalogError,
        match=r"criterion_freeze_catalog_templates_required:got_str",
    ):
        _plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=catalog, graph=None)


def test_criterion_freeze_bad_ref_raises() -> None:
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    spec = TaskSpec.model_construct(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=["not-a-ref"],
    )
    with pytest.raises(CatalogError, match=r"criterion_freeze_bad_ref:str"):
        _plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=None, graph=None)


def test_criterion_freeze_empty_template_id_raises() -> None:
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    spec = TaskSpec.model_construct(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef.model_construct(template_id="")],
    )
    with pytest.raises(CatalogError, match=r"criterion_freeze_empty_template_id"):
        _plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=None, graph=None)


def test_criterion_freeze_existing_attrs_not_mapping_raises() -> None:
    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    sid = "s"
    key = "tmpl_x"
    cnid = criterion_node_id(sid, key)
    graph = GraphStore(default_execution_registry())
    graph.upsert_node(
        GraphNode.model_construct(
            node_id=cnid,
            kind="criterion",
            study_id=sid,
            ref_id=key,
            attrs="not-a-mapping",
            created_at=datetime.now(timezone.utc),
        )
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id=key)],
    )
    with pytest.raises(
        CatalogError,
        match=r"criterion_freeze_existing_attrs_not_mapping:tmpl_x:str",
    ):
        _plan_accept_freeze_rows(sid=sid, tasks=[spec], catalog=None, graph=graph)


def test_criterion_freeze_rubric_required_raises() -> None:
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import Catalog, CodeCheck, RequirementTemplate
    from coscientist.core.plan.lifecycle import _plan_accept_freeze_rows

    check = CodeCheck.model_construct(
        kind="rubric",
        rubric=None,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    tmpl = RequirementTemplate.model_construct(
        id="rub_needed",
        title="w",
        text_ru="w",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        check=check,
        role="validity",
        required=True,
        version=1,
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
        exempt_from_freeze=False,
    )
    catalog = Catalog.model_construct(
        catalog_version=4,
        edition="t",
        sources_sha256={"t": "abc"},
        rules=[],
        templates=[tmpl],
        defaults={},
        code_output_names=[],
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="rub_needed")],
    )
    with pytest.raises(CatalogError, match=r"criterion_freeze_rubric_required:rub_needed"):
        _plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=catalog, graph=None)


def test_criterion_freeze_rubric_digest_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.core.claims.rubric_types import Rubric, RubricItem
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import Catalog, CodeCheck, RequirementTemplate
    from coscientist.core.plan import lifecycle as life

    rub = Rubric(
        items=[RubricItem(id="a", text="A", required=True, check="judge")],
        aggregate="all_required",
    )
    check = CodeCheck.model_construct(
        kind="rubric",
        rubric=rub,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    tmpl = RequirementTemplate.model_construct(
        id="rub_dig",
        title="w",
        text_ru="w",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        check=check,
        role="validity",
        required=True,
        version=1,
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
        exempt_from_freeze=False,
    )
    catalog = Catalog.model_construct(
        catalog_version=4,
        edition="t",
        sources_sha256={"t": "abc"},
        rules=[],
        templates=[tmpl],
        defaults={},
        code_output_names=[],
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="rub_dig")],
    )

    def _empty_digest(r: object) -> object:
        return SimpleNamespace(digest="")

    monkeypatch.setattr("coscientist.core.claims.rubric.freeze_rubric", _empty_digest)
    with pytest.raises(CatalogError, match=r"criterion_freeze_rubric_digest_required:rub_dig"):
        life._plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=catalog, graph=None)


def test_criterion_freeze_variant_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.core.claims.graph import GraphSchemaError
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import Catalog, CodeCheck, RequirementTemplate
    from coscientist.core.plan import lifecycle as life

    check = CodeCheck.model_construct(
        kind="artifact_exists",
        metric="m",
        op="==",
        target=1,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    tmpl = RequirementTemplate.model_construct(
        id="var_x",
        title="w",
        text_ru="w",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        check=check,
        role="validity",
        required=True,
        version=1,
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
        exempt_from_freeze=False,
    )
    catalog = Catalog.model_construct(
        catalog_version=4,
        edition="t",
        sources_sha256={"t": "abc"},
        rules=[],
        templates=[tmpl],
        defaults={},
        code_output_names=[],
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="var_x")],
    )

    def _boom(*_a: object, **_k: object) -> set[str]:
        raise GraphSchemaError("no unique variant match (matched=0)")

    monkeypatch.setattr(life, "required_attr_names", _boom)
    with pytest.raises(
        CatalogError,
        match=r"criterion_freeze_variant:var_x:no unique variant match",
    ):
        life._plan_accept_freeze_rows(sid="s", tasks=[spec], catalog=catalog, graph=None)


@pytest.mark.asyncio
async def test_known_question_texts_refuses_none_and_non_list() -> None:
    from coscientist.core.plan.lifecycle import LifecycleMixin

    class _RT(LifecycleMixin):
        def __init__(self) -> None:
            self.plans = {"p1": {"questions": None}}
            self.store = SimpleNamespace(get_projection=lambda *_a, **_k: [])

    rt = _RT()
    with pytest.raises(ValueError, match=r"plan questions required \(got None\)"):
        await rt._known_question_texts()

    rt.plans = {"p1": {"questions": {"id": "Q1"}}}
    with pytest.raises(ValueError, match=r"plan questions must be a list, got dict"):
        await rt._known_question_texts()
