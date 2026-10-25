"""macaque #41 closeout — one regression test per closed item.

Every test here fails on the previous tip (9e2abd6) for the item it names.
Real-journal tests go through ``SqliteEventStore`` + ``open_runtime`` (the
production replay / upcast path), not a direct upcaster call.
"""

from __future__ import annotations

import dataclasses
import errno
import inspect
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import coscientist.checks.catalog as catalog_mod
import coscientist.checks.kinds._util as util
import coscientist.checks.kinds.executable as ex
import coscientist.checks.sandbox as sb
import coscientist.checks.sandbox_limits as sl
import coscientist.checks.sandbox_probe as probe
from coscientist.checks.catalog import CATALOG_VERSION, load_catalog
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import CatalogError, SandboxInfraError
from coscientist.core.events.events import (
    STUDY_CREATED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    FrozenCheckLimits,
    HitlPolicy,
    RequirementRef,
    RequirementTemplate,
    ReviewCheckPlain,
    Study,
    TaskSpec,
)
from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.checks.sandbox_test_limits import (
    TEST_CHECK_DRAIN_JOIN_S,
    TEST_CHECK_REAP_TIMEOUT_S,
    TEST_PIDS_MAX,
    TEST_RLIMIT_NPROC,
    sandbox_limits,
)
from tests.helpers import close_runtime, open_runtime, seed_legacy_journal


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _row(**over: object) -> dict:
    row: dict = {
        "timeout_s": 5.0,
        "memory_mb": 64,
        "max_output_bytes": 1024,
        "pids_max": 16,
        "rlimit_nproc": 16,
        "reap_timeout_s": 1.0,
        "output_drain_join_s": 1.0,
        "allow_network": False,
        "allow_cgroup_degraded": False,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# domain.py
# ---------------------------------------------------------------------------


def _study(**over: object) -> Study:
    base: dict = dict(
        study_id="s",
        profile="research",
        request="q",
        catalog_version=4,
        hitl_policy=HitlPolicy(),
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        require_hypotheses=False,
    )
    base.update(over)
    return Study(**base)


def test_study_catalog_version_is_strict_int():
    assert _study().catalog_version == 4
    for bad in ("3", "0.1.0", 3.0, True, None):
        with pytest.raises(ValidationError, match="catalog_version"):
            _study(catalog_version=bad)


def test_catalog_catalog_version_is_strict_int():
    assert Catalog(catalog_version=4, edition="e").catalog_version == 4
    for bad in ("3", 3.0, True):
        with pytest.raises(ValidationError, match="catalog_version"):
            Catalog(catalog_version=bad, edition="e")


def test_required_domain_fields_have_no_defaults():
    for name in ("allow_network", "allow_cgroup_degraded"):
        assert CodeCheck.model_fields[name].is_required()
    assert RequirementTemplate.model_fields["exempt_from_freeze"].is_required()
    with pytest.raises(ValidationError, match="exempt_from_freeze"):
        RequirementTemplate(
            id="t",
            title="t",
            text_ru="t",
            method_sources=["m"],
            rule_ids=["R"],
            applies_to=["experiment"],
            check=ReviewCheckPlain(measured=False, reviewer_role="critic", question="q", max_calls=1),
            role="validity",
            version=1,
        )  # type: ignore[call-arg]


def test_task_spec_freeze_map_is_typed_not_any_dict():
    spec = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        sandbox_check_limits={"exec_ok": _row()},
    )
    row = spec.sandbox_check_limits["exec_ok"]  # type: ignore[index]
    assert isinstance(row, FrozenCheckLimits)
    assert row.allow_network is False and row.reap_timeout_s == 1.0
    assert set(FrozenCheckLimits.model_fields) == {
        "timeout_s",
        "memory_mb",
        "max_output_bytes",
        "pids_max",
        "rlimit_nproc",
        "reap_timeout_s",
        "output_drain_join_s",
        "allow_network",
        "allow_cgroup_degraded",
    }
    assert all(f.is_required() for f in FrozenCheckLimits.model_fields.values())
    with pytest.raises(ValidationError):
        FrozenCheckLimits(**_row(), surprise=1)  # type: ignore[call-arg]
    bad = _row()
    del bad["pids_max"]
    with pytest.raises(ValidationError, match="pids_max"):
        FrozenCheckLimits(**bad)


# ---------------------------------------------------------------------------
# upcasters.py / events.py: real journal replay of TaskCreated schema_version=13
# ---------------------------------------------------------------------------


async def _seed_study_with_v13_task(
    tmp_path: Path, clock: FakeClock, sid: str, spec_patch: dict
) -> None:
    """Create a study at current schemas, then append a raw TaskCreated v13 row."""
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    await close_runtime(rt)
    db = tmp_path / sid / "events.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE type='TaskCreated' ORDER BY seq"
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0])
    payload["task_id"] = f"{sid}/E9"
    payload["kind"] = "experiment"
    payload["spec"].update(
        {
            "task_key": "E9",
            "title": "e9",
            "goal": "g",
            "route": "experiment",
            "depends_on": [],
        }
    )
    payload["spec"].pop("sandbox_check_limits", None)
    payload["spec"].update(spec_patch)
    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("seeder")
    await seed_legacy_journal(
        store,
        [
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=f"{sid}/E9",
                idem_key=make_idem("TaskCreated", f"{sid}/E9"),
                payload=payload,
                ts=clock.now(),
                schema_version=13,
            )
        ],
    )
    await store.release_lease("seeder", store.epoch)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec_patch", "match"),
    [
        ({}, "sandbox_check_limits_required"),
        ({"sandbox_check_limits": None}, "sandbox_check_limits_required"),
        ({"sandbox_check_limits": {}}, "sandbox_check_limits_empty"),
        ({"sandbox_check_limits": []}, "sandbox_check_limits_must_be_object"),
        (
            {"sandbox_check_limits": {"exec_ok": {"timeout_s": 1.0}}},
            "missing_security_toggles",
        ),
        (
            {
                "sandbox_check_limits": {
                    "exec_ok": {"allow_network": "false", "allow_cgroup_degraded": False}
                }
            },
            "bad_security_toggle",
        ),
        (
            {
                "sandbox_check_limits": {
                    "exec_ok": {"allow_network": False, "allow_cgroup_degraded": True}
                }
            },
            "freeze_row_invalid",
        ),
    ],
)
async def test_real_replay_v13_task_created_refused_with_catalog_error(
    tmp_path, clock, spec_patch, match
):
    sid = "st_v13_bad"
    await _seed_study_with_v13_task(tmp_path, clock, sid, spec_patch)
    with pytest.raises(CatalogError, match=match):
        await open_runtime(tmp_path, sid, clock=clock, owner="t2")


@pytest.mark.asyncio
async def test_real_replay_v13_task_created_complete_freeze_opens_typed(tmp_path, clock):
    sid = "st_v13_ok"
    await _seed_study_with_v13_task(
        tmp_path, clock, sid, {"sandbox_check_limits": {"exec_ok": _row()}}
    )
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    try:
        row = rt.tasks[f"{sid}/E9"].spec.sandbox_check_limits["exec_ok"]  # type: ignore[index]
        assert isinstance(row, FrozenCheckLimits)
        assert row.pids_max == 16
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_real_replay_v13_empty_freeze_map_refused_at_upcast(tmp_path, clock):
    """Empty {} on a v13 journal is refused at v14 upcast (not later at apply)."""
    sid = "st_v13_empty"
    await _seed_study_with_v13_task(tmp_path, clock, sid, {"sandbox_check_limits": {}})
    with pytest.raises(CatalogError, match="sandbox_check_limits_empty"):
        await open_runtime(tmp_path, sid, clock=clock, owner="t2")


def test_empty_freeze_map_refused_on_replay_to_v14():
    """Empty ``{}`` is refused when replaying into the v14 TaskCreated contract."""
    from coscientist.core.events.upcasters import upcast
    from tests.checks.sandbox_test_limits import exec_ok_freeze_map

    with pytest.raises(CatalogError, match="sandbox_check_limits_empty"):
        upcast(
            "TaskCreated",
            13,
            {
                "task_id": "t",
                "kind": "experiment",
                "spec": {
                    "task_key": "E0",
                    "title": "t",
                    "goal": "g",
                    "route": "experiment",
                    "sandbox_check_limits": {},
                },
                "state": "PENDING",
                "deadline": "2020-01-01T00:00:00+00:00",
            },
        )
    version, payload = upcast(
        "TaskCreated",
        13,
        {
            "task_id": "t",
            "kind": "experiment",
            "spec": {
                "task_key": "E0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
                "sandbox_check_limits": exec_ok_freeze_map(),
            },
            "state": "PENDING",
            "deadline": "2020-01-01T00:00:00+00:00",
        },
    )
    assert version == 14
    assert "exec_ok" in payload["spec"]["sandbox_check_limits"]


def test_writer_constant_and_upcaster_registration_are_unconditional():
    from coscientist.core.events import upcasters as up

    assert TASK_CREATED_SCHEMA_VERSION == 14
    assert 13 in up.UPCASTERS["TaskCreated"]
    src = inspect.getsource(up)
    assert "TASK_CREATED_SCHEMA_VERSION" not in src
    assert 'register_upcaster("TaskCreated", 13, ' in src


@pytest.mark.asyncio
async def test_real_replay_v12_task_created_without_limits_refused(tmp_path, clock):
    """≤v12 TaskCreated lacking sandbox_check_limits → typed CatalogError at open."""
    sid = "st_v12_nolim"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    await close_runtime(rt)
    db = tmp_path / sid / "events.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE type='TaskCreated' ORDER BY seq"
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0])
    payload["task_id"] = f"{sid}/E12"
    payload["kind"] = "experiment"
    payload["spec"].update(
        {
            "task_key": "E12",
            "title": "e12",
            "goal": "g",
            "route": "experiment",
            "depends_on": [],
        }
    )
    payload["spec"].pop("sandbox_check_limits", None)
    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("seeder")
    await seed_legacy_journal(
        store,
        [
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=f"{sid}/E12",
                idem_key=make_idem("TaskCreated", f"{sid}/E12"),
                payload=payload,
                ts=clock.now(),
                schema_version=12,
            )
        ],
    )
    await store.release_lease("seeder", store.epoch)
    await store.close()
    with pytest.raises(CatalogError, match="sandbox_check_limits_required"):
        await open_runtime(tmp_path, sid, clock=clock, owner="t2")


def test_study_created_v12_upcaster_refuses_missing_and_non_int():
    from coscientist.core.events.upcasters import _study_created_v12_to_v13_catalog_version

    with pytest.raises(CatalogError, match="catalog_version_required"):
        _study_created_v12_to_v13_catalog_version({})
    with pytest.raises(CatalogError, match="catalog_version_required"):
        _study_created_v12_to_v13_catalog_version({"catalog_version": None})
    with pytest.raises(CatalogError, match="catalog_version_must_be_strict_int_got_str"):
        _study_created_v12_to_v13_catalog_version({"catalog_version": "0.1.0"})
    assert _study_created_v12_to_v13_catalog_version({"catalog_version": 4})[
        "catalog_version"
    ] == 4
    assert STUDY_CREATED_SCHEMA_VERSION == writer_schema_version("StudyCreated")


def test_create_study_body_requires_strict_int_catalog_version():
    from coscientist.api.studies import CreateStudyBody

    with pytest.raises(ValidationError, match="catalog_version"):
        CreateStudyBody(
            request="q",
            profile="research",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
        )
    with pytest.raises(ValidationError, match="catalog_version"):
        CreateStudyBody.model_validate(
            {
                "request": "q",
                "profile": "research",
                "catalog_version": "3",
                "require_hypotheses": False,
                "confirmations_needed": 1,
                "refutations_needed": 1,
                "refutation_priority": False,
                "supported_needed": 2,
                "max_expansions": 2,
            }
        )
    body = CreateStudyBody(
        request="q",
        profile="research",
        catalog_version=4,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
    )
    assert body.catalog_version == 4


def test_limits_from_check_refuses_int_for_float_fields():
    with pytest.raises(CatalogError, match="sandbox_limits_bad_type:timeout_s:int"):
        sb.limits_from_check(
            timeout_s=1,  # type: ignore[arg-type]
            memory_mb=64,
            max_output_bytes=1000,
            pids_max=16,
            rlimit_nproc=16,
            allow_network=False,
            allow_cgroup_degraded=False,
            reap_timeout_s=1.0,
            output_drain_join_s=1.0,
        )


def test_rubric_code_item_missing_code_kind_refused_not_defaulted(monkeypatch):
    """No soft default to artifact_exists — missing code_kind → FAILED."""
    from coscientist.checks.kinds import rubric as rubric_mod
    from coscientist.checks.kinds.rubric import evaluate_rubric
    from coscientist.core.claims.rubric import freeze_rubric
    from coscientist.core.claims.rubric_types import Rubric, RubricItem
    from coscientist.core.execution.domain import AgentTaskOutput, ReqStatus

    # RubricItem itself refuses missing code_kind when check=code.
    with pytest.raises(ValidationError, match="code_kind"):
        RubricItem(id="a", text="A", required=True, check="code", code_kind=None)

    # Runtime gate: bypass freeze/validator to prove evaluate_rubric does not
    # invent artifact_exists.
    ok = freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="a", text="A", required=True, check="code", code_kind="artifact_exists"
                )
            ],
            aggregate="all_required",
        )
    )
    bypass = Rubric.model_construct(
        items=[
            RubricItem.model_construct(
                id="a", text="A", required=True, check="code", code_kind=None
            )
        ],
        aggregate="all_required",
        k=None,
        digest=ok.digest,
    )
    monkeypatch.setattr(rubric_mod, "_as_rubric", lambda _raw: bypass)
    check = CodeCheck(
        kind="rubric",
        output="main",
        rubric=ok,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    cr = evaluate_rubric(
        check,
        template_id="t",
        check_id="t#1",
        out=AgentTaskOutput(summary="x", outputs={}),
        artifacts=MagicMock(),
        role="validity",
        task_id="st/E1",
        attempt_id="st/E1#1",
        output_digest="a" * 64,
    )
    assert cr.status == ReqStatus.FAILED
    assert "missing_code_kind" in (cr.detail or "")


# ---------------------------------------------------------------------------
# catalog.py / PLAN_N_CRITIC_CRITERIA
# ---------------------------------------------------------------------------


def test_plan_n_freeze_skip_is_exact_set_from_critic_criteria():
    """PLAN-n skip = membership in PLAN_N_CRITIC_CRITERIA (no startswith)."""
    expected = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)
    assert sl.PLAN_N_CRITIC_TEMPLATE_IDS == expected
    assert all(sl.is_plan_n_critic_template_id(tid) for tid in expected)
    assert not sl.is_plan_n_critic_template_id("artifact_present")
    assert not sl.is_plan_n_critic_template_id("plan_expansion_not_in_criteria")
    cat = minimal_test_catalog()
    frozen = sl.freeze_task_sandbox_limits(
        TaskSpec(
            task_key="PLAN-1",
            title="t",
            goal="g",
            route="planner",
            criteria=[RequirementRef(template_id="plan_expansion_schema")],
        ),
        cat,
    )
    assert "plan_expansion_schema" not in (frozen.sandbox_check_limits or {})


def test_production_catalog_module_hosts_no_fixture():
    assert not hasattr(catalog_mod, "minimal_test_catalog")
    assert not hasattr(catalog_mod, "_tmpl")
    assert "M0 test" not in inspect.getsource(catalog_mod)
    assert minimal_test_catalog().edition == "M0 test"


def test_catalog_token_constants_are_single_named_finals():
    assert catalog_mod.LOG_SNIPPET_BYTES == 8000
    for name in (
        "LOG_SNIPPET_BYTES",
        "BUILTIN_TESTS_PASS_ARGV",
        "BUILTIN_METRIC_SCORER_ARGV",
        "VERIFIER_SCORER_GUEST_PATH",
        "GUEST_PYTHON_ARGV0",
    ):
        assert f"{name}: Final" in inspect.getsource(catalog_mod)
    src = inspect.getsource(ex)
    assert "8000" not in src
    assert '"catalog:pytest"' not in src and '"catalog:metric"' not in src
    assert '"/scorer"' not in src and '"python3"' not in src


def test_legacy_string_catalog_version_refused_with_versioned_reason():
    blob = {
        "catalog_version": "0.2.0",
        "edition": "e",
        "sources_sha256": {},
        "rules": [],
        "templates": [],
        "defaults": {},
        "code_output_names": [],
    }
    with pytest.raises(CatalogError) as ei:
        load_catalog(blob, kind_registry=default_execution_registry())
    msg = str(ei.value)
    assert "catalog_version_must_be_strict_int_got_str" in msg
    assert "'0.2.0'" in msg
    assert "supported=[4]" in msg
    assert f"history={catalog_mod.LEGACY_STRING_CATALOG_VERSION_REFUSED}" in msg
    with pytest.raises(CatalogError, match="unsupported_catalog_version:2"):
        load_catalog({**blob, "catalog_version": 2}, kind_registry=default_execution_registry())


def _sesoi_blob(**t_over: object) -> dict:
    tmpl = {
        "id": "s",
        "title": "s",
        "text_ru": "s",
        "method_sources": ["m"],
        "rule_ids": ["R23"],
        "applies_to": ["experiment"],
        "role": "prediction",
        "required": True,
        "version": 1,
        "uses_sesoi": True,
        "predicts_null": False,
        "predicted_direction": "two_sided",
        "exempt_from_freeze": False,
        "check": {
            "kind": "value_matches",
            "output": "main",
            "pointer": "/x",
            "target": 1,
            "allow_network": False,
            "allow_cgroup_degraded": False,
        },
    }
    tmpl.update(t_over)
    return {
        "catalog_version": 4,
        "edition": "e",
        "sources_sha256": {},
        "rules": [{"rule_id": "R23", "source": "r", "excerpt": "x", "digest": "d"}],
        "templates": [tmpl],
        "defaults": {},
        "code_output_names": [],
    }


def test_sesoi_outside_catalog_bounds_refuses_never_clamps():
    reg = default_execution_registry()
    ok = load_catalog(_sesoi_blob(sesoi=1.0, sesoi_min=0.5, sesoi_max=2.0), kind_registry=reg)
    assert ok.templates[0].sesoi == 1.0
    with pytest.raises(CatalogError, match="sesoi_below_min"):
        load_catalog(_sesoi_blob(sesoi=0.1, sesoi_min=0.5, sesoi_max=2.0), kind_registry=reg)
    with pytest.raises(CatalogError, match="sesoi_above_max"):
        load_catalog(_sesoi_blob(sesoi=9.0, sesoi_min=0.5, sesoi_max=2.0), kind_registry=reg)
    with pytest.raises(CatalogError, match="sesoi_must_be_number_got_str"):
        load_catalog(_sesoi_blob(sesoi="1"), kind_registry=reg)
    src = inspect.getsource(catalog_mod.load_catalog)
    assert 'if "sesoi' not in src.replace("uses_sesoi", "")  # helper, no ternaries
    assert '"sesoi" in t else None' not in src


def test_executable_catalog_check_requires_host_budgets():
    reg = default_execution_registry()
    chk = {
        "kind": "exec_runs",
        "argv": ["/bin/true"],
        "timeout_s": 5.0,
        "memory_mb": 64,
        "max_output_bytes": 1000,
        "pids_max": 16,
        "rlimit_nproc": 16,
        "allow_network": False,
        "allow_cgroup_degraded": False,
    }
    with pytest.raises(CatalogError, match="exec_runs:reap_timeout_s_required"):
        load_catalog(_sesoi_blob(check=chk, uses_sesoi=False), kind_registry=reg)
    with pytest.raises(CatalogError, match="exec_runs:output_drain_join_s_required"):
        load_catalog(
            _sesoi_blob(check={**chk, "reap_timeout_s": 1.0}, uses_sesoi=False),
            kind_registry=reg,
        )


def test_catalog_host_tuning_constants_replace_sandbox_defaults():
    assert not [n for n in dir(sb) if n.startswith("DEFAULT_")]
    for n in (
        "SANDBOX_TERM_GRACE_S",
        "SANDBOX_WATCH_POLL_INTERVAL_S",
        "SANDBOX_CGROUP_CPU_MAX",
        "PROBE_NET_CANARY_HOST",
    ):
        assert hasattr(catalog_mod, n)
    assert not hasattr(catalog_mod, "SANDBOX_REAP_TIMEOUT_S")


# ---------------------------------------------------------------------------
# sandbox_limits.py
# ---------------------------------------------------------------------------


def _exec_check(**over: object) -> dict:
    check = {
        "kind": "exec_runs",
        "argv": ["/bin/true"],
        "timeout_s": 5.0,
        "memory_mb": 64,
        "max_output_bytes": 1000,
        "pids_max": TEST_PIDS_MAX,
        "rlimit_nproc": TEST_RLIMIT_NPROC,
        "reap_timeout_s": 2.5,
        "output_drain_join_s": 1.5,
        "allow_network": False,
        "allow_cgroup_degraded": True,
    }
    check.update(over)
    return check


def _cat(defaults: dict[str, list[str]], *, with_proposed: bool = False) -> Catalog:
    tmpls = [
        {
            "id": "exec_ok",
            "title": "e",
            "text_ru": "e",
            "method_sources": ["m"],
            "rule_ids": ["R23"],
            "applies_to": ["experiment"],
            "role": "validity",
            "required": True,
            "version": 1,
            "uses_sesoi": False,
            "predicts_null": False,
            "predicted_direction": "two_sided",
            "exempt_from_freeze": False,
            "check": _exec_check(),
        }
    ]
    if with_proposed:
        tmpls.append(
            {
                **tmpls[0],
                "id": "proposed_check",
                "check": {
                    "kind": "artifact_exists",
                    "output": "main",
                    "allow_network": False,
                    "allow_cgroup_degraded": False,
                },
            }
        )
    blob = _sesoi_blob()
    blob["templates"] = tmpls
    blob["defaults"] = defaults
    return load_catalog(blob, kind_registry=default_execution_registry())


def test_freeze_row_copies_catalog_check_verbatim_and_nothing_is_derived():
    cat = _cat({}, with_proposed=True)
    out = sl.freeze_task_sandbox_limits(
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            criteria=[RequirementRef(template_id="exec_ok")] * 2,
        ),
        cat,
    )
    rows = out.sandbox_check_limits
    assert set(rows) == {"exec_ok", "proposed_check"}  # type: ignore[arg-type]
    assert rows["exec_ok"] == FrozenCheckLimits(**_row(  # type: ignore[index]
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=1000,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        reap_timeout_s=2.5,
        output_drain_join_s=1.5,
        allow_cgroup_degraded=True,
    ))
    # Non-sandbox kind: budgets the check does not carry stay None (no tuning).
    pc = rows["proposed_check"]  # type: ignore[index]
    assert pc.reap_timeout_s is None and pc.output_drain_join_s is None
    assert pc.timeout_s is None


def test_freeze_empty_criteria_without_experiment_defaults_adds_no_default_rows():
    cat = _cat({})
    out = sl.freeze_task_sandbox_limits(
        TaskSpec(task_key="E1", title="t", goal="g", route="experiment"), cat
    )
    assert out.sandbox_check_limits == {}
    with_default = _cat({"experiment": ["exec_ok"]})
    out2 = sl.freeze_task_sandbox_limits(
        TaskSpec(task_key="E1", title="t", goal="g", route="experiment"), with_default
    )
    assert set(out2.sandbox_check_limits) == {"exec_ok"}  # type: ignore[arg-type]
    assert "defaults.get(" not in inspect.getsource(sl)


def test_freeze_bad_default_template_id_is_catalog_error():
    cat = _cat({"experiment": ["exec_ok"]})
    broken = cat.model_copy(update={"defaults": {"experiment": [""]}})
    with pytest.raises(CatalogError, match="bad_default_template_id"):
        sl.freeze_task_sandbox_limits(
            TaskSpec(task_key="E1", title="t", goal="g", route="experiment"), broken
        )


def test_apply_frozen_limits_applies_every_frozen_value_incl_none():
    cat = _cat({})
    spec = sl.freeze_task_sandbox_limits(
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            criteria=[RequirementRef(template_id="exec_ok")],
        ),
        cat,
    )
    agent_check = CodeCheck(
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=999.0,
        memory_mb=9999,
        max_output_bytes=10**9,
        pids_max=10**6,
        rlimit_nproc=10**6,
        reap_timeout_s=99.0,
        output_drain_join_s=99.0,
        allow_network=True,
        allow_cgroup_degraded=True,
    )
    applied = sl.apply_frozen_limits(agent_check, spec, template_id="exec_ok")
    assert (applied.timeout_s, applied.memory_mb, applied.reap_timeout_s) == (5.0, 64, 2.5)
    assert applied.output_drain_join_s == 1.5 and applied.allow_network is False


# ---------------------------------------------------------------------------
# executable.py / _util.py
# ---------------------------------------------------------------------------


def _cc(**over: object) -> CodeCheck:
    fields: dict = dict(
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=1000,
        pids_max=16,
        rlimit_nproc=16,
        reap_timeout_s=1.0,
        output_drain_join_s=1.0,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    fields.update(over)
    return CodeCheck(**fields)


def test_agent_filename_allowed_is_exact_no_basename_stripping():
    allow = frozenset({"solution.py"})
    assert ex.agent_filename_allowed("solution.py", allow)
    for bad in ("sub/solution.py", "../solution.py", "/solution.py", "", ".", ".."):
        assert not ex.agent_filename_allowed(bad, allow)


def test_input_allowlist_entries_must_be_plain_filenames():
    assert ex._input_allowlist(_cc(input_allowlist=["a.py"])) == frozenset({"a.py"})
    assert ex._input_allowlist(_cc()) == frozenset()
    with pytest.raises(CatalogError, match="input_allowlist_entry_not_plain_filename"):
        ex._input_allowlist(_cc(input_allowlist=["dir/a.py"]))
    with pytest.raises(CatalogError, match="input_allowlist_entry_not_plain_filename"):
        ex._input_allowlist(_cc(input_allowlist=[".."]))


def test_argv_and_command_are_mutually_exclusive():
    assert ex._argv_for(_cc(argv=["a", "b"])) == ["a", "b"]
    assert ex._argv_for(_cc(argv=None, command="/bin/x")) == ["/bin/x"]
    assert ex._argv_for(_cc(argv=None)) == []
    with pytest.raises(CatalogError, match="argv_and_command_both_set"):
        ex._argv_for(_cc(argv=["a"], command="b"))


def test_limits_errors_are_catalog_errors_and_nothing_is_defaulted():
    lim = ex._limits(_cc())
    assert lim.reap_timeout_s == 1.0 and lim.output_drain_join_s == 1.0
    for field in ("reap_timeout_s", "output_drain_join_s", "timeout_s", "pids_max"):
        with pytest.raises(CatalogError, match="sandbox_limits_missing_required"):
            ex._limits(_cc(**{field: None}))
    with pytest.raises(CatalogError, match="sandbox_limits_invalid"):
        ex._limits(_cc(timeout_s=0.01, reap_timeout_s=0.01))  # cpu budget < floor


def test_effect_extra_drops_none_only_and_refuses_empty_strings():
    assert ex._effect_extra(("a", None), ("b", 0), ("c", 0.0), ("d", "x")) == {
        "b": 0,
        "c": 0.0,
        "d": "x",
    }
    with pytest.raises(CatalogError, match="effect_extra_empty_string:k"):
        ex._effect_extra(("k", ""))


def test_tests_pass_launcher_refuses_unimportable_agent_module_names():
    src = ex._tests_pass_launcher().decode()
    assert 'raise ImportError("agent module name not importable' in src
    assert 'isidentifier() or name.startswith("_"):\n            continue' not in src


def test_catalog_scorer_guest_argv_comes_from_catalog_tokens(monkeypatch, tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle

    seen: dict = {}

    def fake_run_verifier(argv, **kw):
        seen["argv"] = list(argv)
        seen["kw"] = kw
        raise RuntimeError("stop")

    monkeypatch.setattr(ex, "run_verifier", fake_run_verifier)
    arts = LocalArtifactStore(tmp_path)
    meta = arts.put(
        b"def f():\n    return 1\n",
        make_tool_ref("st/E1#1:code"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(
        outputs={"solution.py": ArtifactHandle(artifact_id=meta.artifact_id, name="solution.py")}
    )
    check = _cc(
        kind="tests_pass",
        argv=list(catalog_mod.BUILTIN_TESTS_PASS_ARGV),
        tests={"test_x.py": "def test_x():\n    assert True\n"},
        input_allowlist=["solution.py"],
        min_tests=1,
    )
    with pytest.raises(RuntimeError, match="stop"):
        ex.evaluate_tests_pass(
            check,
            template_id="tp",
            check_id="tp#0",
            out=out,
            artifacts=arts,
            role="validity",
            attempt_id="st/E1#1",
        )
    assert seen["argv"] == [
        catalog_mod.GUEST_PYTHON_ARGV0,
        "-I",
        catalog_mod.VERIFIER_SCORER_GUEST_PATH,
    ]
    assert seen["kw"]["extra_ro_files"] == {}
    assert seen["kw"]["input_allowlist"] == frozenset({"solution.py"})


def test_rerun_isolation_must_not_change_between_runs(monkeypatch, tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import AgentTaskOutput

    calls = {"n": 0}

    def fake_run_verifier(argv, **kw):
        calls["n"] += 1
        return sb.SandboxResult(
            status=sb.SandboxStatus.OK,
            exit_code=0,
            stdout=b"",
            stderr=b"",
            reason="ok",
            duration_s=0.1,
            cgroup_limits_held=True,
            net_isolation="hard",
            fs_isolation="hard" if calls["n"] == 1 else "soft",
            oom_detectable=True,
            result_files={ex.DIGEST_NAME: b'{"schema": "coscientist.reward.v1", "value": 1}'},
            note=None,
        )

    monkeypatch.setattr(ex, "run_verifier", fake_run_verifier)
    check = _cc(kind="reproducible_rerun", rerun_tol=0.1)
    with pytest.raises(SandboxInfraError, match="isolation_changed_between_reruns"):
        ex.evaluate_reproducible_rerun(
            check,
            template_id="rr",
            check_id="rr#0",
            out=AgentTaskOutput(),
            artifacts=LocalArtifactStore(tmp_path),
            role="validity",
            attempt_id="a1",
            prov_fields={},
        )
    assert calls["n"] == 2


def test_require_isolation_validates_tokens_without_cast():
    for ok in ("hard", "soft", "none"):
        assert sb.require_isolation(ok) == ok
    for bad in ("HARD", "", None, 1, "hard "):
        with pytest.raises(SandboxInfraError, match="bad_isolation_token"):
            sb.require_isolation(bad)
    assert "cast(" not in inspect.getsource(ex)


def test_raise_if_infra_requires_explicit_note_and_adds_none():
    with pytest.raises(TypeError):
        util.raise_if_infra(  # type: ignore[call-arg]
            sb.SandboxStatus.UNAVAILABLE, "r", net_isolation="soft", fs_isolation="soft"
        )
    with pytest.raises(SandboxInfraError) as ei:
        util.raise_if_infra(
            sb.SandboxStatus.UNAVAILABLE,
            "reason_x",
            net_isolation="soft",
            fs_isolation="soft",
            note=None,
        )
    assert str(ei.value) == "reason_x"
    util.raise_if_infra(
        sb.SandboxStatus.OK, "ok", net_isolation="soft", fs_isolation="soft", note=None
    )


def test_parse_junit_has_no_silent_node_skips():
    ok = (
        b'<testsuites><testsuite tests="2" failures="1" errors="0"/>'
        b'<testsuite tests="1" failures="0" errors="1"/></testsuites>'
    )
    assert util.parse_junit(ok) == (2, 3)
    assert util.parse_junit(b'<testsuite tests="4" failures="0" errors="0"/>') == (0, 4)
    with pytest.raises(ValueError, match="junit_unexpected_root:foo"):
        util.parse_junit(b'<foo tests="1" failures="0" errors="0"/>')
    with pytest.raises(ValueError, match="junit_unexpected_node:properties"):
        util.parse_junit(
            b'<testsuites><properties/><testsuite tests="1" failures="0" errors="0"/></testsuites>'
        )
    with pytest.raises(ValueError, match="junit_missing_attr:errors"):
        util.parse_junit(b'<testsuite tests="1" failures="0"/>')


def test_guest_timeout_result_requires_timeout_status_and_explicit_args():
    ok = sb.SandboxResult(
        status=sb.SandboxStatus.OK,
        exit_code=0,
        stdout=b"",
        stderr=b"",
        reason="ok",
        duration_s=0.1,
        cgroup_limits_held=True,
        net_isolation="hard",
        fs_isolation="hard",
        oom_detectable=True,
    )
    with pytest.raises(SandboxInfraError, match="non_timeout_status"):
        util.guest_timeout_result(
            check_id="c",
            template_id="t",
            role="validity",
            attempt_id="a",
            result=ok,
            evidence=[],
            jail_inputs_digest=None,
        )
    with pytest.raises(TypeError):
        util.guest_timeout_result(  # type: ignore[call-arg]
            check_id="c", template_id="t", role="validity", attempt_id="a", result=ok
        )


def test_merge_numeric_extra_kind_from_catalog_token():
    assert util.merge_numeric_extra(None, est=1.0)["kind"] == catalog_mod.EFFECT_EST_KIND_POINT
    assert '"point"' not in inspect.getsource(util.merge_numeric_extra)


# ---------------------------------------------------------------------------
# sandbox.py
# ---------------------------------------------------------------------------


def test_sandbox_limits_and_result_have_no_defaulted_policy_fields():
    lim_fields = {f.name: f for f in dataclasses.fields(sb.SandboxLimits)}
    defaulted = {
        n
        for n, f in lim_fields.items()
        if f.default is not dataclasses.MISSING
        or f.default_factory is not dataclasses.MISSING
    }
    assert defaulted == {"sigxcpu_slack_s", "sigxcpu_min_measured_s"}  # derived exactly
    res_fields = {f.name: f for f in dataclasses.fields(sb.SandboxResult)}
    for n in ("net_isolation", "fs_isolation", "oom_detectable", "cgroup_limits_held"):
        assert res_fields[n].default is dataclasses.MISSING
    with pytest.raises(TypeError):
        sb.SandboxLimits(  # type: ignore[call-arg]
            timeout_s=1.0,
            memory_mb=64,
            max_output_bytes=1,
            pids_max=1,
            rlimit_nproc=1,
            allow_network=False,
            allow_cgroup_degraded=False,
        )
    with pytest.raises(TypeError, match="cgroup_cpu_max"):
        sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=1, cgroup_cpu_max=5)


def test_plain_filename_gate_is_exact_and_run_sandbox_refuses_path_names(tmp_path):
    allow = frozenset({"a.py", "../a.py", "d/a.py"})
    assert sb.agent_input_name_allowed("a.py", allow)
    for bad in ("../a.py", "d/a.py", "", ".", "..", "a\x00.py"):
        assert not sb.agent_input_name_allowed(bad, allow)
    assert sb.is_plain_filename("x.json") and not sb.is_plain_filename("x/y")
    if sb.bwrap_available() and probe.probe_bwrap_hard(allow_network=False):
        with pytest.raises(SandboxInfraError, match="input_name_rejected"):
            sb.run_sandbox(
                ["/bin/true"],
                limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
                inputs={"sub/escape.py": b"x"},
                env={},
                cwd=tmp_path,
            )
        with pytest.raises(SandboxInfraError, match="input_name_rejected"):
            sb.run_verifier(
                ["/bin/true"],
                inputs={"sub/a.py": b"x"},
                limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
                collect=[],
                extra_ro_files={},
                input_allowlist=frozenset({"a.py"}),
            )
        with pytest.raises(SandboxInfraError, match="extra_input_name_rejected"):
            sb.run_verifier(
                ["/bin/true"],
                inputs={},
                limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
                collect=[],
                extra_ro_files={"zzz.py": b"x"},
                input_allowlist=frozenset({"a.py"}),
            )
    with pytest.raises(TypeError):
        sb.run_verifier(  # type: ignore[call-arg]
            ["/bin/true"],
            inputs={},
            limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
            collect=[],
        )


def test_collect_results_refuses_path_like_names(tmp_path):
    (tmp_path / "ok.txt").write_bytes(b"x")
    assert sb._collect_results(tmp_path, ["ok.txt"]) == {"ok.txt": b"x"}
    for bad in ("../ok.txt", "d/ok.txt", ""):
        with pytest.raises(SandboxInfraError, match="collect_name_rejected"):
            sb._collect_results(tmp_path, [bad])


def test_read_core_output_removed():
    assert not hasattr(sb, "read_core_output")


def test_cgroup_job_not_stripped_or_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CGROUP_JOB", "")
    with pytest.raises(SandboxInfraError, match="cgroup_job_empty"):
        sb._cgroup_delegation_ceiling()
    monkeypatch.setenv("CGROUP_JOB", f" {tmp_path} ")
    with pytest.raises(SandboxInfraError, match="cgroup_job_whitespace_padded"):
        sb._cgroup_delegation_ceiling()


def test_cgroup_prepare_failure_tags_use_named_constant_and_report_cleanup(
    monkeypatch, tmp_path
):
    assert sb.MISSING_LIMIT_CGROUP == "cgroup"
    monkeypatch.setattr(
        sb, "_cgroup_nest_root", lambda: (_ for _ in ()).throw(SandboxInfraError("x"))
    )
    prep = sb._cgroup_prepare_leaf(sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1))
    assert prep.missing == (sb.MISSING_LIMIT_CGROUP,)
    assert '("cgroup",)' not in inspect.getsource(sb)


def test_cgroup_cleanup_failures_are_reported_not_swallowed(tmp_path, caplog):
    import logging

    leaf = tmp_path / "leaf"
    (leaf / "nested").mkdir(parents=True)  # rmdir fails: directory not empty
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox"):
        note = sb._cgroup_remove_leaf(leaf)
    assert note is not None and note.startswith("cleanup_failed:cgroup_rmdir:leaf:")
    assert any("cleanup_failed" in m for m in caplog.messages)
    assert sb._cgroup_remove_leaf(None) is None
    clean = tmp_path / "clean"
    clean.mkdir()
    assert sb._cgroup_remove_leaf(clean) is None and not clean.exists()
    prep_src = inspect.getsource(sb._cgroup_prepare_leaf)
    assert "pass  #" not in prep_src


def test_close_fd_logs_instead_of_swallowing(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox"):
        sb._close_fd(10**6, "unit")
    assert any("close unit fd=1000000" in m for m in caplog.messages)
    src = inspect.getsource(sb._run_jailed)
    assert "pass  #" not in src


def test_killpg_sigkill_reports_non_lookup_failures(monkeypatch):
    import os

    assert sb._killpg_sigkill(2**22 + 12345) is None  # no such group: done

    def deny(*_a):
        raise PermissionError(errno.EPERM, "no")

    monkeypatch.setattr(os, "killpg", deny)
    assert sb._killpg_sigkill(1) == "killpg_oserror:PermissionError"


def test_infra_kind_must_be_ascii():
    proc = MagicMock()
    proc.returncode = 75
    kw = dict(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        out_b=b"",
        err_b=b"",
        duration=0.1,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        lim=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
        cgroup_limits_held=False,
    )
    ok = sb._classify(infra_msg=sb._INFRA_MAGIC + b"rlimit\n", **kw)  # type: ignore[arg-type]
    assert ok.reason == "sandbox_unavailable:rlimit"
    with pytest.raises(SandboxInfraError, match="infra_kind_not_ascii"):
        sb._classify(infra_msg=sb._INFRA_MAGIC + b"r\xff\xfe\n", **kw)  # type: ignore[arg-type]


def _classify_kw(tmp_path, *, leaf: bool, **over):
    proc = MagicMock()
    proc.returncode = over.pop("code", 0)
    cg = None
    if leaf:
        cg = tmp_path / "leaf"
        cg.mkdir(parents=True, exist_ok=True)
        (cg / "memory.events").write_text(over.pop("memory_events", "oom_kill 0\n"))
    kw = dict(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=cg,
        oom_detectable=leaf,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=1.0,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        lim=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
        cgroup_limits_held=leaf,
    )
    kw.update(over)
    return kw


@pytest.mark.parametrize("code", [-9, 137])
def test_oom_undetectable_sigkill_is_infra_not_failed(tmp_path, code):
    with pytest.raises(SandboxInfraError, match="oom_undetectable_sigkill"):
        sb._classify(**_classify_kw(tmp_path, leaf=False, code=code))
    # Verified cgroup: same exit is scientific unless memory.events says OOM.
    r = sb._classify(**_classify_kw(tmp_path, leaf=True, code=code))
    assert r.status is sb.SandboxStatus.NONZERO
    oom = sb._classify(
        **_classify_kw(tmp_path / "o", leaf=True, code=code, memory_events="oom_kill 1\n")
    )
    assert oom.status is sb.SandboxStatus.OOM


def test_degraded_nonkill_exit_stays_scientific(tmp_path):
    r = sb._classify(**_classify_kw(tmp_path, leaf=False, code=1))
    assert r.status is sb.SandboxStatus.NONZERO
    assert r.oom_detectable is False


def test_timeout_cpu_source_is_fixed_by_mode_never_fallback(tmp_path):
    common = dict(timed_out=True, kill_cause="wall", code=-9)
    # No cgroup: only the host-tree sample counts; a cgroup-sourced value alone
    # (impossible without a leaf) is not a stand-in.
    with pytest.raises(SandboxInfraError, match="missing_cpu_usage"):
        sb._classify(**_classify_kw(tmp_path, leaf=False, cpu_usage_s=0.5, **common))
    r = sb._classify(**_classify_kw(tmp_path, leaf=False, rusage_cpu_s=0.5, **common))
    assert r.cpu_usage_s == 0.5 and r.reason == "wall_timeout"
    # Live cgroup: cpu.stat wins, the rusage sample is ignored.
    r2 = sb._classify(
        **_classify_kw(tmp_path / "l", leaf=True, cpu_usage_s=0.01, rusage_cpu_s=4.0, **common)
    )
    assert r2.cpu_usage_s == 0.01 and r2.reason == "idle_timeout"


def test_cgroup_procs_bad_token_is_infra_not_filtered(tmp_path):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text("67108864")
    (leaf / "cgroup.procs").write_text("12 abc\n")
    with pytest.raises(SandboxInfraError, match="cgroup_procs_bad_token"):
        sb._verify_guest_in_cgroup(
            leaf, 1, lim=sandbox_limits(timeout_s=1.0, memory_mb=64, max_output_bytes=1),
            bwrap_exited=lambda: False,
        )


@pytest.mark.parametrize(
    ("peak", "match"),
    [(None, "pids_peak_FileNotFoundError"), ("junk", "pids_peak_parse"), ("1", "pids_peak=1")],
)
def test_exited_bwrap_without_peak_proof_is_infra_not_verified(tmp_path, peak, match):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text("67108864")
    (leaf / "cgroup.procs").write_text("")
    if peak is not None:
        (leaf / "pids.peak").write_text(peak)
    with pytest.raises(SandboxInfraError, match=match):
        sb._verify_guest_in_cgroup(
            leaf, 1, lim=sandbox_limits(timeout_s=1.0, memory_mb=64, max_output_bytes=1),
            bwrap_exited=lambda: True,
        )
    (leaf / "pids.peak").write_text("2\n")
    assert sb._verify_guest_in_cgroup(
        leaf, 1, lim=sandbox_limits(timeout_s=1.0, memory_mb=64, max_output_bytes=1),
        bwrap_exited=lambda: True,
    ) == (True, None)


def _install_fake_proc(monkeypatch, tree: dict[str, str | OSError]):
    real = Path.read_text

    def fake_read(self, *a, **k):
        v = tree.get(str(self))
        if v is None:
            return real(self, *a, **k)
        if isinstance(v, OSError):
            raise v
        return v

    monkeypatch.setattr(Path, "read_text", fake_read)


def test_host_tree_cpu_missing_children_file_is_infra_not_undercount(monkeypatch):
    monkeypatch.setattr(sb.os, "listdir", lambda p: ["4242"] if p == "/proc/4242/task" else [])
    _install_fake_proc(
        monkeypatch,
        {
            "/proc/4242/task/4242/children": FileNotFoundError(errno.ENOENT, "x"),
        },
    )
    real_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: True if str(self) == "/proc/4242/task/4242" else real_exists(self),
    )
    with pytest.raises(SandboxInfraError, match="proc_children_unavailable"):
        sb._host_tree_cpu_s(4242)


def test_host_tree_cpu_vanished_pid_skipped_but_other_errors_are_infra(monkeypatch):
    tick = sb._clk_tck()
    stat = "4242 (py thon) S " + " ".join(["0"] * 10) + " 7 3 " + "0 " * 30
    monkeypatch.setattr(sb.os, "listdir", lambda p: ["4242"] if p == "/proc/4242/task" else [])
    _install_fake_proc(
        monkeypatch,
        {
            "/proc/4242/task/4242/children": "9999 ",
            "/proc/9999/stat": FileNotFoundError(errno.ENOENT, "gone"),
            "/proc/4242/stat": stat,
        },
    )
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: real_exists(self))
    assert sb._host_tree_cpu_s(4242) == pytest.approx(10 / tick)
    _install_fake_proc(
        monkeypatch,
        {
            "/proc/4242/task/4242/children": "",
            "/proc/4242/stat": PermissionError(errno.EACCES, "no"),
        },
    )
    with pytest.raises(SandboxInfraError, match="host_tree_cpu:PermissionError"):
        sb._host_tree_cpu_s(4242)
    _install_fake_proc(monkeypatch, {"/proc/4242/task/4242/children": "x1"})
    with pytest.raises(SandboxInfraError, match="bad_children_token"):
        sb._host_tree_cpu_s(4242)
    src = inspect.getsource(sb._host_tree_cpu_s)
    assert "continue" not in src and "silently" not in src


def test_wrapper_exec_memory_error_is_infra_channel_not_scientific_exit(tmp_path):
    lim = sandbox_limits(timeout_s=30.0, memory_mb=2048, max_output_bytes=1000)
    script = sb._wrapper_script(lim=lim, cgroup_live=False)
    assert "stays scientific" not in script
    assert "sys.exit(1)" not in script.split("except MemoryError:")[1].split("except OSError")[0]
    import os

    r_fd, w_fd = os.pipe()
    harness = (
        "import os\n"
        "def _boom(*a, **k):\n    raise MemoryError()\n"
        "os.execvp = _boom\n"
        f"exec(compile({script!r}, 'wrapper', 'exec'))\n"
    )
    env = {"COSCIENTIST_INFRA_FD": str(w_fd), "PATH": os.environ["PATH"]}
    p = subprocess.run(
        ["python3", "-c", harness, "anything"],
        env=env,
        pass_fds=(w_fd,),
        capture_output=True,
        timeout=30,
    )
    os.close(w_fd)
    msg = os.read(r_fd, 256)
    os.close(r_fd)
    assert p.returncode == sb._WRAPPER_ABORT_EXIT
    assert msg == sb._INFRA_EXEC_MEMORY


def test_missing_limit_tags_and_scorer_alias_removed_from_source():
    src = inspect.getsource(sb.run_verifier)
    assert 'a == "/scorer"' not in src and '"/scorer"' not in src
    assert "VERIFIER_SCORER_GUEST_PATH" in src
    assert "Path(name).name" not in inspect.getsource(sb)
    assert "x.isdigit()" not in inspect.getsource(sb)
    assert '"replace"' not in inspect.getsource(sb)
    assert "strip()" not in inspect.getsource(sb._cgroup_delegation_ceiling).split("env !=")[0]


def test_run_sandbox_cwd_none_owns_and_removes_tempdir():
    if not (sb.bwrap_available() and probe.probe_bwrap_hard(allow_network=False)):
        pytest.skip("bwrap hard required")
    r = sb.run_sandbox(
        ["/bin/true"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
        inputs={},
        env={},
    )
    assert r.work_dir is not None and not Path(r.work_dir).exists()


# ---------------------------------------------------------------------------
# sandbox_probe.py
# ---------------------------------------------------------------------------


def _fake_probe_run(monkeypatch, *, returncode=None, exc=None, stderr=b""):
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: "/bin/true")
    monkeypatch.setattr(probe, "_common_jail_flags", lambda **_k: [])

    def fake_run(argv, **kw):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, b"", stderr)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)


def test_probe_verdict_codes_are_false_every_other_failure_is_infra(monkeypatch):
    try:
        for code in (catalog_mod.PROBE_EXIT_HOST_VISIBLE, catalog_mod.PROBE_EXIT_NET_UP):
            _fake_probe_run(monkeypatch, returncode=code)
            assert probe.probe_bwrap_hard(allow_network=False) is False
        _fake_probe_run(monkeypatch, returncode=0)
        assert probe.probe_bwrap_hard(allow_network=False) is True
        _fake_probe_run(monkeypatch, returncode=1, stderr=b"bwrap: userns denied")
        with pytest.raises(SandboxInfraError, match=r"bwrap_probe_exit:1:.*userns denied"):
            probe.probe_bwrap_hard(allow_network=False)
        _fake_probe_run(monkeypatch, exc=subprocess.TimeoutExpired("bwrap", 1))
        with pytest.raises(SandboxInfraError, match="bwrap_probe_failed:TimeoutExpired"):
            probe.probe_bwrap_hard(allow_network=False)
        _fake_probe_run(monkeypatch, exc=OSError("nope"))
        with pytest.raises(SandboxInfraError, match="bwrap_probe_failed:OSError"):
            probe.probe_bwrap_hard(allow_network=False)
    finally:
        probe.probe_bwrap_hard.cache_clear()


def test_probe_infra_failure_is_not_cached_as_a_verdict(monkeypatch):
    try:
        _fake_probe_run(monkeypatch, returncode=1)
        with pytest.raises(SandboxInfraError):
            probe.probe_bwrap_hard(allow_network=False)
        _fake_probe_run(monkeypatch, returncode=0)  # clears cache; same call now passes
        assert probe.probe_bwrap_hard(allow_network=False) is True
    finally:
        probe.probe_bwrap_hard.cache_clear()


def test_probe_constants_come_from_catalog_and_literals_are_gone():
    assert probe.PROBE_NET_CANARY_HOST is catalog_mod.PROBE_NET_CANARY_HOST
    assert catalog_mod.PROBE_NET_CANARY_HOST == "1.1.1.1"
    src = inspect.getsource(probe)
    assert "1.1.1.1" not in src and "/etc/hostname" not in src
    assert 'allow_network else "soft"' not in src
    assert "r.stderr or" not in src


def test_isolation_without_hard_is_an_explicit_table(monkeypatch):
    assert probe._NET_ISOLATION_WITHOUT_HARD == {True: "none", False: "soft"}
    monkeypatch.setattr(probe, "bwrap_available", lambda: False)
    assert probe.resolve_isolation(allow_network=True) == ("none", "soft")
    assert probe.resolve_isolation(allow_network=False) == ("soft", "soft")
    monkeypatch.setattr(probe, "bwrap_available", lambda: True)
    monkeypatch.setattr(probe, "probe_bwrap_hard", lambda *, allow_network: False)
    assert probe.resolve_isolation(allow_network=True) == ("none", "soft")
    assert probe.resolve_isolation(allow_network=False) == ("soft", "soft")


def test_site_packages_equal_to_workspace_is_refused_not_skipped(monkeypatch, tmp_path):
    import site

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path)])
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path / "absent"))
    with pytest.raises(SandboxInfraError, match="site_packages_is_workspace"):
        probe._python_site_ro_binds()
    other = tmp_path / "site"
    other.mkdir()
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(other), str(other)])
    args, path = probe._python_site_ro_binds()
    assert args == ["--ro-bind", str(other), str(other)] and path == str(other)


def test_verifier_scorer_mount_uses_catalog_guest_path(tmp_path):
    scorer = tmp_path / "s.py"
    scorer.write_text("x")
    if not sb.bwrap_available():
        pytest.skip("bwrap required to build argv")
    argv = probe.build_verifier_bwrap(
        results=tmp_path, inputs=tmp_path, scorer=scorer, holdout=None, allow_network=False
    )
    i = argv.index(str(scorer))
    assert argv[i + 1] == catalog_mod.VERIFIER_SCORER_GUEST_PATH
    assert ["--setenv", "COSCIENTIST_SCORER", catalog_mod.VERIFIER_SCORER_GUEST_PATH] == argv[
        argv.index("COSCIENTIST_SCORER") - 1 : argv.index("COSCIENTIST_SCORER") + 2
    ]


# ---------------------------------------------------------------------------
# tests/ fixture hygiene + collateral call sites
# ---------------------------------------------------------------------------


def test_sandbox_limits_helper_supplies_every_tuning_field_explicitly():
    lim = sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=1)
    assert lim.term_grace_s == catalog_mod.SANDBOX_TERM_GRACE_S
    assert lim.cgroup_cpu_max == catalog_mod.SANDBOX_CGROUP_CPU_MAX
    assert lim.reap_timeout_s == TEST_CHECK_REAP_TIMEOUT_S
    assert lim.output_drain_join_s == TEST_CHECK_DRAIN_JOIN_S


def test_limits_from_check_builds_from_catalog_tuning_for_non_check_fields():
    lim = sb.limits_from_check(
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=1000,
        pids_max=8,
        rlimit_nproc=8,
        allow_network=False,
        allow_cgroup_degraded=False,
        reap_timeout_s=0.5,
        output_drain_join_s=0.25,
    )
    for field, const in (
        ("term_grace_s", "SANDBOX_TERM_GRACE_S"),
        ("watch_poll_interval_s", "SANDBOX_WATCH_POLL_INTERVAL_S"),
        ("cgroup_probe_max_attempts", "SANDBOX_CGROUP_PROBE_MAX_ATTEMPTS"),
        ("idle_cpu_wall_ratio", "SANDBOX_IDLE_CPU_WALL_RATIO"),
        ("cgroup_cpu_max", "SANDBOX_CGROUP_CPU_MAX"),
    ):
        assert getattr(lim, field) == getattr(catalog_mod, const)
    assert (lim.reap_timeout_s, lim.output_drain_join_s) == (0.5, 0.25)


def test_study_catalog_version_str_refused_on_real_create_study(tmp_path, clock):
    import asyncio

    async def go():
        rt = await open_runtime(tmp_path, "st_str_cv", clock=clock, owner="t1")
        try:
            # Typed fail at create_study entry (before Study validate).
            with pytest.raises(CatalogError, match="catalog_version_must_be_strict_int"):
                await rt.create_study(
                    profile="research",
                    request="q",
                    catalog_version="0.1.0",  # type: ignore[arg-type]
                    deadline=clock.now() + timedelta(seconds=60),
                    sid="st_str_cv",
                    finalize_reserve_s=5,
                    require_hypotheses=False,
                    confirmations_needed=1,
                    refutations_needed=1,
                    refutation_priority=False,
                    supported_needed=2,
                    max_expansions=2,
                    inputs=[],
                )
        finally:
            await close_runtime(rt)

    asyncio.run(go())



def test_guest_never_inherits_infra_fd_even_though_wrapper_keeps_it_until_exec():
    if not (sb.bwrap_available() and probe.probe_bwrap_hard(allow_network=False)):
        pytest.skip("bwrap hard required")
    r = sb.run_sandbox(
        [
            "python3",
            "-c",
            "import os;print(sorted(int(x) for x in os.listdir('/proc/self/fd')));"
            "print(os.environ.get('COSCIENTIST_INFRA_FD'))",
        ],
        limits=sandbox_limits(timeout_s=10, memory_mb=256, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    assert r.status is sb.SandboxStatus.OK
    fds, env_fd = r.stdout.decode().splitlines()
    assert env_fd == "None"
    # 0/1/2 plus the transient fd of the listdir itself — never the infra pipe.
    assert json.loads(fds)[:3] == [0, 1, 2] and len(json.loads(fds)) <= 4
    assert "set_inheritable" in sb._wrapper_script(
        lim=sandbox_limits(timeout_s=10, memory_mb=256, max_output_bytes=1), cgroup_live=False
    )
