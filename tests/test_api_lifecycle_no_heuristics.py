"""PR2 API/lifecycle/Study: fail-closed no-hardcodes regressions.

Each test fails without the corresponding fix in studies.py / lifecycle.py /
domain.py (Study / GateConfig / plan_timeout_s / OSError reclaim).
"""

from __future__ import annotations

import inspect
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from coscientist.api import studies as studies_mod
from coscientist.api.studies import (
    _PEER_WAIT_FLOOR_S,
    CreateStudyBody,
    _pid_alive,
    _reservation_reclaimable,
    _write_owner_sidecar,
    post_studies,
)
from coscientist.core.execution.domain import (
    DEFAULT_GATE_TIMEOUT_S,
    GATE_TIMEOUTS_S,
    RESEARCH_PROFILE,
    GateConfig,
    HitlPolicy,
    Study,
    research_profile_create_kwargs,
)
from coscientist.core.plan import lifecycle as lifecycle_mod
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime, open_runtime

# --------------------------------------------------------------------------
# domain.py — Study / GateConfig required (rule 12 / field_default)
# --------------------------------------------------------------------------


def test_study_research_quotas_are_required_no_pydantic_invent():
    required = (
        "finalize_reserve_s",
        "confirmations_needed",
        "refutations_needed",
        "refutation_priority",
        "supported_needed",
        "aggregate",
        "evalue_alpha",
        "max_expansions",
        "max_human_expansions",
        "replan_reserve",
        "max_tasks",
        "max_depth",
        "plan_timeout_s",
        "max_foreach_children",
        "max_loop_iter",
        "max_task_budget_share",
        "max_proposals",
    )
    for name in required:
        assert Study.model_fields[name].is_required(), name
    # Missing keys filled from RESEARCH_PROFILE (before-validator), never Field invents.
    filled = Study(
        study_id="s",
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert filled.finalize_reserve_s == RESEARCH_PROFILE.finalize_reserve_s
    assert filled.plan_timeout_s == RESEARCH_PROFILE.plan_timeout_s
    # Explicit zeros/False must not be overwritten by the profile fill.
    explicit = Study(
        study_id="s",
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        finalize_reserve_s=0,
        refutation_priority=False,
        max_expansions=0,
    )
    assert explicit.finalize_reserve_s == 0
    assert explicit.max_expansions == 0


def test_gate_config_timeout_required_and_from_gate_timeouts_map():
    assert GateConfig.model_fields["timeout_s"].is_required()
    assert GateConfig.model_fields["enabled"].is_required()
    assert GateConfig.model_fields["on_timeout"].is_required()
    with pytest.raises(ValidationError):
        GateConfig(enabled=True, on_timeout="delegate")  # type: ignore[call-arg]
    assert GATE_TIMEOUTS_S["spec_approval"] == 120
    assert GATE_TIMEOUTS_S["plan"] == 180
    assert DEFAULT_GATE_TIMEOUT_S == 1800
    policy = HitlPolicy()
    assert policy.gate("hypotheses").timeout_s == GATE_TIMEOUTS_S["hypotheses"]
    with pytest.raises(KeyError, match="unknown HITL gate kind"):
        policy.gate("not_a_gate")


def test_research_profile_is_single_source_for_create_study_defaults():
    caps = research_profile_create_kwargs()
    assert caps["finalize_reserve_s"] == RESEARCH_PROFILE.finalize_reserve_s == 30
    assert caps["plan_timeout_s"] == RESEARCH_PROFILE.plan_timeout_s == 600
    body = CreateStudyBody(request="q", catalog_version=3)
    assert body.profile == "research"
    assert body.finalize_reserve_s == RESEARCH_PROFILE.finalize_reserve_s
    assert body.plan_timeout_s == RESEARCH_PROFILE.plan_timeout_s
    # Wire body must not re-invent a second literal 30 / 600 Field default.
    src = Path(studies_mod.__file__).read_text(encoding="utf-8")
    assert "finalize_reserve_s: int = 30" not in src
    assert "research_profile_create_kwargs()" in src


# --------------------------------------------------------------------------
# studies.py — OSError reclaim fail-closed, peer wait floor, no or ""
# --------------------------------------------------------------------------


def test_pid_alive_oserror_treated_as_alive_fail_closed_reclaim():
    """Unknown OSError must not unlock a live neighbour (fail-closed)."""

    def boom(_pid: int, _sig: int) -> None:
        raise OSError("probe failed")

    with patch.object(os, "kill", side_effect=boom):
        assert _pid_alive(12345) is True


def test_reservation_not_reclaimed_when_pid_probe_raises_oserror(tmp_path: Path):
    path = tmp_path / "idem"
    path.write_bytes(b"{partial")
    _write_owner_sidecar(path, hostname=socket.gethostname(), owner_pid=os.getpid())
    os.utime(path, (os.path.getmtime(path), os.path.getmtime(path)))

    def boom(_pid: int, _sig: int) -> None:
        raise OSError("probe failed")

    with patch.object(os, "kill", side_effect=boom):
        # Own pid probe fails with OSError → treat alive → not reclaimable.
        assert _reservation_reclaimable(path) is False


def test_peer_wait_floor_is_named_constant_not_literal_30():
    assert _PEER_WAIT_FLOOR_S == 30.0
    src = inspect.getsource(studies_mod.post_studies)
    assert "30.0" not in src
    assert "_PEER_WAIT_FLOOR_S" in src


def test_hostname_mapping_no_or_empty_invent():
    src = Path(studies_mod.__file__).read_text(encoding="utf-8")
    assert 'str(data.get("hostname") or "")' not in src
    assert 'str(existing.get("hostname") or "")' not in src
    assert "_hostname_from_mapping" in src


@pytest.mark.asyncio
async def test_post_studies_passes_plan_timeout_s_from_body(tmp_path: Path):
    catalog = minimal_test_catalog()
    body = CreateStudyBody(
        request="plan timeout wire",
        catalog_version=3,
        plan_timeout_s=123,
        deadline=datetime(2099, 1, 1, tzinfo=UTC),
    )
    r = await post_studies(tmp_path, body, catalog=catalog)
    rt = await open_runtime(tmp_path, r["study_id"], catalog=catalog)
    try:
        assert rt.study is not None
        assert rt.study.plan_timeout_s == 123
    finally:
        await close_runtime(rt)


# --------------------------------------------------------------------------
# lifecycle.py — plan_timeout_s from study; no getattr or invents
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_plan_deadline_uses_study_plan_timeout_s(tmp_path: Path):
    from coscientist.core.events.clock import FakeClock

    catalog = minimal_test_catalog()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    rt = await open_runtime(tmp_path, "st_plan_to", catalog=catalog, clock=clock)
    try:
        now = clock.now()
        await rt.create_study(
            profile="research",
            request="plan timeout",
            catalog_version=3,
            deadline=now + timedelta(hours=2),
            finalize_reserve_s=30,
            plan_timeout_s=90,
        )
        assert rt.study is not None
        plan_task = next(t for t in rt.tasks.values() if t.spec.task_key == "PLAN")
        expected = min(now + timedelta(seconds=90), rt.study.effective_deadline())
        assert plan_task.deadline == expected
        # Must not use the old hardcoded 600s window.
        assert plan_task.deadline != min(now + timedelta(seconds=600), rt.study.effective_deadline())
    finally:
        await close_runtime(rt)


def test_lifecycle_create_study_caps_come_from_research_profile_not_literals():
    src = inspect.getsource(lifecycle_mod.LifecycleMixin.create_study)
    assert "finalize_reserve_s: int = 30" not in src
    assert "plan_timeout_s: int = 600" not in src
    assert "research_profile_create_kwargs" in src
    assert "timedelta(seconds=600)" not in inspect.getsource(lifecycle_mod.LifecycleMixin.create_study)


def test_lifecycle_accept_plan_no_getattr_or_study_cap_invents():
    src = Path(lifecycle_mod.__file__).read_text(encoding="utf-8")
    assert 'getattr(self.study, "max_loop_iter", 8) or 8' not in src
    assert 'getattr(self.study, "max_depth", 8) or 8' not in src
    assert 'getattr(self.study, "max_tasks", 64) or 64' not in src
    assert 'getattr(q, "final_type", None) or "verdict"' not in src
    assert 'getattr(q, "origin", None) or "explicit"' not in src
    assert 'getattr(rub, "digest", "") or ""' not in src
    assert 'cattrs["alpha"] = 0.05' not in src
    # No executor.catalog alias invent.
    assert 'executor", None), "catalog"' not in src.replace("'", '"')
    assert 'getattr(getattr(self, "executor", None), "catalog", None)' not in src


def test_lifecycle_no_schema_version_numeric_literals_in_accept_plan_writers():
    src = inspect.getsource(lifecycle_mod.LifecycleMixin.accept_plan)
    assert "schema_version=1" not in src
    assert "schema_version=2" not in src
    assert "writer_schema_version" in src
