"""Wired critic + reviewer lanes — plan-quality gate and post-close review (e2e cassette).

Cassette order per run: tz_spec → planner → critic (wired) → executor calls
→ reviewer (wired, post-close). The unwired path consumes no critic/reviewer
bodies (byte-identical behavior with --wired-roles without them).
"""

from __future__ import annotations

import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import TypedDict, TypeVar

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.critic import CriticDecision, CriticIssue, CriticVerdict
from coscientist.agents.prompts.roles import (
    ROLE_CRITIC_REF,
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_REVIEWER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.schemas import ReviewerOut
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy
from coscientist.llm.client import LlmClient
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import _cli_plan_out_json, _spec_json
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_T = TypeVar("_T")

_PROFILE = load_profile("research", expected_tool_catalog_version=2)
_MODEL = _PROFILE.roles["planner"].model


class _UsageRow(TypedDict, total=False):
    """Usage-row keys asserted on below (every run_study row types them as declared)."""

    role: str
    stage: str
    issues: list[str]
    note: str
    artifact_ids: dict[str, str]
    gaps: list[str]
    prompt_tokens: int


class _RunSummary(TypedDict, total=False):
    """Run-summary JSON keys asserted on below."""

    study_id: str
    verdict: str
    usage: list[_UsageRow]


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")

    async def sleep(delay_s: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
        del timeout_s
        return await awaitable

    return LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=LlmFakeClock(),
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )


def _wired_adapter(
    client: LlmClient, router: StaticToolRouter, *, roles: frozenset[str] = frozenset({"critic", "reviewer"})
) -> LlmAgentAdapter:
    role_models = {
        "planner": _MODEL,
        "executor-route": _MODEL,
        "tz_spec": _MODEL,
    }
    role_prompts = {
        "planner": ROLE_PLANNER_REF,
        "executor-route": ROLE_EXECUTOR_REF,
        "tz_spec": ROLE_TZ_SPEC_REF,
    }
    if "critic" in roles:
        role_models["critic"] = _MODEL
        role_prompts["critic"] = ROLE_CRITIC_REF
    if "reviewer" in roles:
        role_models["reviewer"] = _MODEL
        role_prompts["reviewer"] = ROLE_REVIEWER_REF
    return LlmAgentAdapter(
        client,
        role_models=role_models,
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts=role_prompts,
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )


def _critic_body(
    decision: CriticDecision, *, path: str = "EXP-0", message: str = "plan issue"
) -> bytes:
    verdict = CriticVerdict(
        decision=decision,
        issues=() if decision == "accept" else (CriticIssue(path=path, message=message),),
    )
    return _ok_body(content=verdict.model_dump_json(), prompt_tokens=8, completion_tokens=3)


def _reviewer_body() -> bytes:
    review = ReviewerOut(
        assessment="оба задания завершены; выводы следуют из наблюдений дайджеста",
        gaps=("нет отрицательного контроля",),
    )
    return _ok_body(content=review.model_dump_json(), prompt_tokens=9, completion_tokens=4)


def _study_bodies() -> list[bytes]:
    return [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2
        ),
    ]


def _plan_body() -> bytes:
    return _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20)


def _arts_meta(data_root: Path, study_id: str) -> dict[str, str]:
    """artifact name → markdown body (via meta JSON + CAS blob)."""
    out: dict[str, str] = {}
    meta_dir = data_root / study_id / "arts" / "meta"
    for meta_file in sorted(meta_dir.glob("*.json")):
        row = json.loads(meta_file.read_text(encoding="utf-8"))
        out[str(row["name"])] = (data_root / study_id / "arts" / "cas" / str(row["sha256"])).read_text(
            encoding="utf-8"
        )
    return out


async def _run_wired(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    wired: frozenset[str],
    adapter_roles: frozenset[str] = frozenset({"critic", "reviewer"}),
    profile_roles_stripped: frozenset[str] = frozenset(),
    study_id: str = "roles_wiring_study",
) -> tuple[int, _RunSummary]:
    task = tmp_path / "task.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(
        tools={
            "dataset_overview": {"result": {"ok": True}, "args": {}},
            "chemical_space_clustering": {"result": {"ok": True}, "args": {}},
        }
    )
    client = _client(bodies, monkeypatch=monkeypatch)
    profile = (
        _PROFILE
        if not profile_roles_stripped
        else _PROFILE.model_copy(
            update={"roles": {k: v for k, v in _PROFILE.roles.items() if k not in profile_roles_stripped}}
        )
    )
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=profile,
        owner="roles-wiring-test",
        adapter=_wired_adapter(client, router, roles=adapter_roles),
        out=out,
        study_id=study_id,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        finalize_reserve_s=30,
        hitl_policy=HitlPolicy(mode="headless"),
        lease_ttl_s=60.0,
        max_llm_calls=100,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        tools=router,
        wired_roles=wired,
        study_clock=FakeClock(),
    )
    return code, json.loads(out.getvalue().strip())


@pytest.mark.asyncio
async def test_wired_critic_accept_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Critic accept → accept_plan as today: 1 critic call, 1 planner call, fulfilled."""
    code, summary = await _run_wired(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=[*_study_bodies()[:2], _critic_body("accept"), *_study_bodies()[2:]],
        wired=frozenset({"critic"}),
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    critic_rows = [u for u in summary["usage"] if u.get("role") == "critic"]
    planner_rows = [u for u in summary["usage"] if u.get("role") == "planner"]
    assert len(critic_rows) == 1
    assert len(planner_rows) == 1


@pytest.mark.asyncio
async def test_wired_critic_issues_trigger_one_planner_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic revise → ONE planner round with the issues as plan_refusal_hint, then accept."""
    bodies = [
        *_study_bodies()[:2],
        _critic_body("revise", message="overview does not address the request"),
        _plan_body(),
        _critic_body("accept"),
        *_study_bodies()[2:],
    ]
    code, summary = await _run_wired(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, wired=frozenset({"critic"})
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    planner_rows = [u for u in summary["usage"] if u.get("role") == "planner"]
    critic_rows = [u for u in summary["usage"] if u.get("role") == "critic"]
    assert len(planner_rows) == 2  # initial + one bounded replan round
    assert len(critic_rows) == 2  # one per planning cycle (≤2 per cycle)
    # The single replan study carries the run: no -r1 study was needed (in-place round).
    assert summary["study_id"] == "roles_wiring_study"


@pytest.mark.asyncio
async def test_wired_critic_second_failure_journaled_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issues after the one replan round → plan accepted WITH issues journaled (never silent)."""
    bodies = [
        *_study_bodies()[:2],
        _critic_body("revise", message="first issue"),
        _plan_body(),
        _critic_body("revise", message="second issue"),
        *_study_bodies()[2:],
    ]
    code, summary = await _run_wired(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, wired=frozenset({"critic"})
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    journal_rows = [
        u
        for u in summary["usage"]
        if isinstance(u, dict) and u.get("role") == "critic" and u.get("stage") == "plan"
    ]
    assert len(journal_rows) == 1
    assert journal_rows[0]["issues"] == ["EXP-0: second issue"]
    assert "never silent" in str(journal_rows[0]["note"])


@pytest.mark.asyncio
async def test_unwired_run_makes_no_critic_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unwired run: byte-identical cassette (no critic bodies) closes as before."""
    code, summary = await _run_wired(
        tmp_path, monkeypatch=monkeypatch, bodies=_study_bodies(), wired=frozenset()
    )
    assert code == 0
    roles = [str(u.get("role")) for u in summary["usage"]]
    assert "critic" not in roles
    assert "reviewer" not in roles


@pytest.mark.asyncio
async def test_wired_reviewer_lane_writes_review_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fulfilled run + wired reviewer → CAS review.md + honest usage row; verdict untouched."""
    code, summary = await _run_wired(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=[*_study_bodies(), _reviewer_body()],
        wired=frozenset({"reviewer"}),
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    arts = _arts_meta(tmp_path / "data", "roles_wiring_study")
    assert "review.md" in arts
    assert "оба задания завершены" in arts["review.md"]
    assert "нет отрицательного контроля" in arts["review.md"]
    assert "Дайджест графа" in arts["review.md"]  # digest embedded as the fact source
    review_rows = [u for u in summary["usage"] if u.get("role") == "reviewer"]
    assert len(review_rows) == 1
    row = review_rows[0]
    assert set(row["artifact_ids"]) == {"review.md"}
    assert row["gaps"] == ["нет отрицательного контроля"]
    assert row["prompt_tokens"] > 0


@pytest.mark.asyncio
async def test_wired_critic_without_adapter_role_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--wired-roles critic + adapter built without the critic role → typed refuse (code 5)."""
    with pytest.raises(CliRefuse, match="critic declared in --wired-roles") as ei:
        await _run_wired(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            wired=frozenset({"critic"}),
            adapter_roles=frozenset(),
        )
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_wired_critic_without_profile_role_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--wired-roles critic + profile without a critic role → typed refuse (code 5)."""
    with pytest.raises(CliRefuse, match="has no critic role") as ei:
        await _run_wired(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            wired=frozenset({"critic"}),
            profile_roles_stripped=frozenset({"critic"}),
        )
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_wired_reviewer_without_profile_role_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--wired-roles reviewer + profile without a reviewer role → typed refuse (code 5)."""
    with pytest.raises(CliRefuse, match="has no reviewer role") as ei:
        await _run_wired(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            wired=frozenset({"reviewer"}),
            profile_roles_stripped=frozenset({"reviewer"}),
        )
    assert ei.value.code == 5
