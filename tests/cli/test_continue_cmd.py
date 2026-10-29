"""CLI ``continue`` — journal reopen/resume semantics with fakes (gap №4).

Checkpoint-equals design under test: the journal is the checkpoint; restore =
replay + lease takeover (epoch/nonce fencing); continue = ``recover()`` +
re-drive through the same dispatch helpers as ``run``. The kill/restore e2e
(SIGKILL between processes) lives in ``test_continue_e2e_slow.py``.
"""

from __future__ import annotations

import io
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.continue_cmd import (
    continue_study,
    draft_from_task_spec,
    spec_redrivable,
)
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.cli.study_helpers import _to_task_spec
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import HitlPolicy, Need, TaskSpec
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import (
    _adapter,
    _cli_plan_out_json,
    _client,
    _default_tools,
    _spec_json,
    _TickingFakeClock,
)
from tests.helpers import close_runtime, open_runtime
from tests.llm.test_client import _ok_body

_RESEARCH_PROFILE = load_profile("research", expected_tool_catalog_version=2)


async def _run_phase(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    study_id: str,
    study_clock: FakeClock,
    max_llm_calls: int = 100,
    router: StaticToolRouter | None = None,
    coder_backend: str | None = None,
    coder_jobs: dict[str, object] | None = None,
) -> tuple[int, dict[str, object] | None, StaticToolRouter]:
    """Run ``run_study`` with the shared two-EXP cassette plan."""
    task = tmp_path / "task.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    r = router if router is not None else StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), r)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-test",
        adapter=adapter,
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
        lease_ttl_s=60.0,
        max_llm_calls=max_llm_calls,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        tools=r,
        hitl_policy=HitlPolicy(mode="headless"),
        study_clock=study_clock,
        coder_backend=coder_backend,  # type: ignore[arg-type]
        coder_jobs=coder_jobs,  # type: ignore[arg-type]
    )
    raw = out.getvalue().strip()
    summary: dict[str, object] | None = dict(json.loads(raw)) if raw else None
    return code, summary, r


async def _continue_phase(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    study_id: str,
    study_clock: FakeClock,
    router: StaticToolRouter,
    lease_wait_s: float = 0.0,
    max_tokens: int = 1_000_000,
    coder_backend: str | None = None,
    coder_jobs: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    out = io.StringIO()
    code = await continue_study(
        data_root=tmp_path / "data",
        study_id=study_id,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-continue",
        adapter_factory=lambda policy: adapter,
        out=out,
        catalog_version=CATALOG_VERSION,
        tools=router,
        lease_ttl_s=60.0,
        lease_wait_s=lease_wait_s,
        max_llm_calls=100,
        max_tokens=max_tokens,
        max_tool_calls=50,
        max_ticks=20,
        study_clock=study_clock,
        coder_backend=coder_backend,
        coder_jobs=coder_jobs,  # type: ignore[arg-type]
    )
    summary = dict(json.loads(out.getvalue().strip()))
    return code, summary


async def _read_events(tmp_path: Path, study_id: str) -> list:
    store = SqliteEventStore(
        study_id, tmp_path / "data" / study_id / "events.db", clock=FakeClock()
    )
    try:
        return list(await store.read_all())
    finally:
        await store.close()


async def _seed_partial_run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    study_id: str,
    clock: FakeClock,
) -> StaticToolRouter:
    """Kill-simulation: EXP-0 recorded, EXP-1 attempt left OPEN mid-flight.

    Phase 1 stops right after the first EXP ``ResultRecorded`` (EXP-0): the
    session LLM budget (``max_llm_calls=3`` = tz_spec + planner + EXP-0) makes
    EXP-1's executor-route run raise the terminal budget ``AgentError`` at the
    gate → typed CliRefuse(4) with the EXP-1 attempt still OPEN — the exact
    journal state a SIGKILL mid-EXP-1 leaves (no AttemptClosed, no result).
    """
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
    ]
    with pytest.raises(CliRefuse, match="budget.max_llm_calls") as ei:
        await _run_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_id=study_id,
            study_clock=clock,
            max_llm_calls=3,
        )
    assert ei.value.code == 4
    events = await _read_events(tmp_path, study_id)
    assert any(e.type == "ResultRecorded" for e in events), "EXP-0 must be recorded"
    exp1_started = [
        e
        for e in events
        if e.type == "AttemptStarted" and str(e.entity_key).endswith("/EXP-1#1")
    ]
    exp1_closed = [
        e
        for e in events
        if e.type == "AttemptClosed" and str(e.entity_key).endswith("/EXP-1#1")
    ]
    assert len(exp1_started) == 1 and not exp1_closed, (
        "EXP-1 attempt#1 must be left OPEN (SIGKILL-equivalent state)"
    )
    return StaticToolRouter(tools=_default_tools())


def _exec_bodies() -> list[bytes]:
    return [
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        )
    ]


# --- fail-closed surface ------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_missing_journal_refuses(tmp_path: Path) -> None:
    with pytest.raises(CliRefuse, match="no journal") as ei:
        await continue_study(
            data_root=tmp_path / "data",
            study_id="ghost",
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="o",
            adapter_factory=lambda p: None,  # type: ignore[arg-type,return-value]
            out=io.StringIO(),
            catalog_version=CATALOG_VERSION,
            tools=StaticToolRouter(tools=_default_tools()),
            lease_ttl_s=60.0,
            lease_wait_s=0.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_continue_journal_foreign_study_id_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Copied journal under another study dir → 0 events for that id → refuse."""
    router = await _seed_partial_run(
        tmp_path, monkeypatch=monkeypatch, study_id="st_a", clock=clock
    )
    shutil.copytree(tmp_path / "data" / "st_a", tmp_path / "data" / "st_b")
    with pytest.raises(CliRefuse, match="no events for study-id 'st_b'") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=_exec_bodies(),
            study_id="st_b",
            study_clock=clock,
            router=router,
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_continue_corrupted_journal_refuses(tmp_path: Path) -> None:
    db = tmp_path / "data" / "st_corrupt" / "events.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"definitely not sqlite")
    with pytest.raises(CliRefuse, match="unreadable") as ei:
        await continue_study(
            data_root=tmp_path / "data",
            study_id="st_corrupt",
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="o",
            adapter_factory=lambda p: None,  # type: ignore[arg-type,return-value]
            out=io.StringIO(),
            catalog_version=CATALOG_VERSION,
            tools=StaticToolRouter(tools=_default_tools()),
            lease_ttl_s=60.0,
            lease_wait_s=0.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_continue_finalized_study_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Full fulfilled run, then continue on the finalized journal → typed refuse."""
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    code, summary, router = await _run_phase(
        tmp_path, monkeypatch=monkeypatch, bodies=bodies, study_id="st_done", study_clock=clock
    )
    assert code == 0
    assert summary is not None and summary["verdict"] == "fulfilled"
    with pytest.raises(CliRefuse, match="already finalized") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            study_id="st_done",
            study_clock=clock,
            router=router,
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_continue_no_accepted_plan_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Killed before PLAN: journal has StudyCreated but no PlanAccepted → refuse."""
    rt = await open_runtime(tmp_path / "data", "st_noplan", clock=clock, owner="seeder")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_noplan",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    await close_runtime(rt)
    with pytest.raises(CliRefuse, match="no accepted plan") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            study_id="st_noplan",
            study_clock=clock,
            router=StaticToolRouter(tools=_default_tools()),
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_continue_catalog_version_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """F3 hardening: a stale ``--catalog-version`` vs the replayed study's
    journaled catalog_version → typed refuse(3) (no silent catalog drift)."""
    rt = await open_runtime(tmp_path / "data", "st_catver", clock=clock, owner="seeder")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION + 1,  # journaled pin differs from the flag
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_catver",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    await close_runtime(rt)
    with pytest.raises(CliRefuse, match="catalog_version") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            study_id="st_catver",
            study_clock=clock,
            router=StaticToolRouter(tools=_default_tools()),
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_continue_live_lease_refuses_fail_closed(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A live (unexpired) predecessor lease + zero wait window → refuse code 5."""
    rt = await open_runtime(tmp_path / "data", "st_live", clock=clock, owner="owner-live")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_live",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    try:
        with pytest.raises(CliRefuse, match="lease held by") as ei:
            await continue_study(
                data_root=tmp_path / "data",
                study_id="st_live",
                catalog=minimal_test_catalog(),
                loaded_profile=_RESEARCH_PROFILE,
                owner="owner-continue",
                adapter_factory=lambda p: None,  # type: ignore[arg-type,return-value]
                out=io.StringIO(),
                catalog_version=CATALOG_VERSION,
                tools=StaticToolRouter(tools=_default_tools()),
                lease_ttl_s=60.0,
                lease_wait_s=0.0,
                max_llm_calls=10,
                max_tokens=1000,
                max_tool_calls=5,
                max_ticks=5,
                study_clock=clock,
            )
        assert ei.value.code == 5
    finally:
        await close_runtime(rt)


# --- the recovery lane core ---------------------------------------------------


@pytest.mark.asyncio
async def test_continue_after_kill_completes_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Kill after first ResultRecorded → continue → fulfilled, unique EXP-n,
    closed attempts never re-executed (attempt ids and results unchanged)."""
    router = await _seed_partial_run(
        tmp_path, monkeypatch=monkeypatch, study_id="st_rec", clock=clock
    )
    before = await _read_events(tmp_path, "st_rec")

    def _attempt_ids(events: list, task_key: str) -> list:
        from coscientist.core.events.ids import task_id as _tid

        tid = _tid("st_rec", task_key)
        return [
            e.entity_key
            for e in events
            if e.type == "AttemptStarted" and e.payload.get("task_id") == tid
        ]

    exp0_attempts_before = _attempt_ids(before, "EXP-0")
    exp1_attempts_before = _attempt_ids(before, "EXP-1")
    results_before = {
        e.entity_key for e in before if e.type == "ResultRecorded"
    }
    assert len(exp0_attempts_before) == 1, "EXP-0 must have exactly one attempt"
    assert len(exp1_attempts_before) == 1, "EXP-1 attempt was started before the kill"

    # TickingFakeClock: recover() closes the crashed EXP-1 attempt retryable
    # (retry_at now+0.05s); the core-fired retry must fire on the next tick —
    # the same composition run uses (#219).
    code, summary = await _continue_phase(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=_exec_bodies(),
        study_id="st_rec",
        study_clock=_TickingFakeClock(),
        router=router,
    )
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"

    after = await _read_events(tmp_path, "st_rec")
    # Unique task keys — no duplicate EXP tasks from the resume.
    created = [
        e.payload.get("task_key") or e.payload.get("spec", {}).get("task_key")
        for e in after
        if e.type == "TaskCreated"
    ]
    created = [c for c in created if c]
    assert len(created) == len(set(created))
    assert set(created) >= {"EXP-0", "EXP-1"}
    # Closed attempts are never re-executed: EXP-0 attempt ids unchanged, and
    # every pre-kill result survives the resume untouched (F2: explicit subset,
    # not a tautology).
    assert _attempt_ids(after, "EXP-0") == exp0_attempts_before
    results_after = {e.entity_key for e in after if e.type == "ResultRecorded"}
    assert results_before <= results_after, "pre-kill results lost by the resume"
    exp0_results = [
        e
        for e in after
        if e.type == "ResultRecorded"
        and e.payload.get("attempt_id") in exp0_attempts_before
    ]
    assert len(exp0_results) == 1, "EXP-0 result must be recorded exactly once"
    # EXP-1 got a fresh attempt (open crashed one closed by recover, then the
    # core-fired retry was executed by the shared helper).
    exp1_attempts_after = _attempt_ids(after, "EXP-1")
    assert exp1_attempts_after[:1] == exp1_attempts_before
    assert len(exp1_attempts_after) == 2
    # Exactly one finalize with a verdict.
    finalized = [e for e in after if e.type == "StudyFinalized"]
    assert len(finalized) == 1
    assert finalized[0].payload.get("verdict") == "fulfilled"


# --- F1: shared-executor-helper failure mapping (composition with run #219) ----


@pytest.mark.asyncio
async def test_continue_transient_executor_failure_retries_via_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Resumed EXP-1 fails transiently (schema-invalid output, repair also
    invalid) → ``AgentError`` is CAUGHT by the shared ``_execute_executor_attempt``
    (no raw traceback), the attempt closes retryable via the core, and the
    core-fired retry completes the study fulfilled — run/continue compose."""
    router = await _seed_partial_run(
        tmp_path, monkeypatch=monkeypatch, study_id="st_transient", clock=clock
    )
    bodies = [
        # Core-fired retry (EXP-1#2): invalid + failed repair → retryable close.
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="still-not-json{{{", prompt_tokens=1, completion_tokens=1),
        # Core-fired retry (EXP-1#3): valid → fulfilled.
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    code, summary = await _continue_phase(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        study_id="st_transient",
        study_clock=_TickingFakeClock(),
        router=router,
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    retry_rows = [u for u in summary["usage"] if "retryable_error" in u]
    assert retry_rows and retry_rows[0]["task_key"] == "EXP-1", (
        f"transient failure not typed into a retryable row: {summary['usage']!r}"
    )
    events = await _read_events(tmp_path, "st_transient")
    exp1_closes = [
        e
        for e in events
        if e.type == "AttemptClosed" and "/EXP-1#" in str(e.entity_key)
    ]
    assert len(exp1_closes) == 3  # recover(crashed #1) + transient #2 + success #3


@pytest.mark.asyncio
async def test_continue_schema_invalid_exhausts_retry_budget_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Executor output invalid on EVERY attempt: the bounded core retry loop
    exhausts ``Study.max_attempts`` → study closes FAILED → typed CliRefuse(7)
    (post-#219 semantics: transient schema failures retry; only budget/drift
    map to rc=4). No raw traceback, no duplicate results, ≤ max_attempts."""
    router = await _seed_partial_run(
        tmp_path, monkeypatch=monkeypatch, study_id="st_exhaust", clock=clock
    )
    bodies = [
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="still-not-json{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="not-json-even-more{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="still-not-json-even-more{{{", prompt_tokens=1, completion_tokens=1),
    ]
    with pytest.raises(CliRefuse, match="verdict='partial'") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_id="st_exhaust",
            study_clock=_TickingFakeClock(),
            router=router,
        )
    assert ei.value.code == 7
    events = await _read_events(tmp_path, "st_exhaust")
    exp1_attempts = [
        e for e in events if e.type == "AttemptStarted" and "/EXP-1#" in str(e.entity_key)
    ]
    assert len(exp1_attempts) == 3, "retry loop must be bounded by Study.max_attempts"
    exp1_results = [
        e for e in events if e.type == "ResultRecorded" and "/EXP-1#" in str(e.entity_key)
    ]
    assert not exp1_results, "a never-valid task must not record a result"
    finalized = [e for e in events if e.type == "StudyFinalized"]
    assert len(finalized) == 1 and finalized[0].payload.get("verdict") == "partial"


@pytest.mark.asyncio
async def test_continue_budget_agent_error_typed_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Budget-exceeded ``AgentError`` mid-resume → typed CliRefuse(4) from the
    shared helper (never a raw traceback; the budget gate never retries)."""
    router = await _seed_partial_run(
        tmp_path, monkeypatch=monkeypatch, study_id="st_budget", clock=clock
    )
    with pytest.raises(CliRefuse, match="agent error: budget.max_tokens") as ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=_exec_bodies(),
            study_id="st_budget",
            study_clock=_TickingFakeClock(),
            router=router,
            max_tokens=1,
        )
    assert ei.value.code == 4


# --- N1: coder re-drive (run parity — coder attempts are never CLI-executed) ---


def _coder_jobs_decl(argv: list[str]) -> dict[str, object]:
    from coscientist.cli.run import _parse_coder_jobs
    from tests.cli.test_run_study import _coder_job_payload

    payload = _coder_job_payload()
    payload["EXP-0"]["argv"] = argv  # type: ignore[index]
    parsed: dict[str, object] = dict(_parse_coder_jobs(payload))
    return parsed


@pytest.mark.asyncio
async def test_continue_coder_task_first_dispatch_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Killed before the coder task's FIRST dispatch (zero attempts — run
    phase refused on a declared-output mismatch, pre-start_attempt) →
    continue dispatches the declared job exactly once → fulfilled."""
    from tests.cli.test_run_study import _coder_plan_json, _coder_router
    from tests.llm.test_client import _ok_body

    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_coder_plan_json(), prompt_tokens=10, completion_tokens=20),
    ]
    # Declared outputs don't cover the derived 'coder' output → run's typed
    # refuse BEFORE start_attempt: coder task exists with zero attempts.
    from coscientist.cli.run import _parse_coder_jobs
    from tests.cli.test_run_study import _coder_job_payload

    wrong_outputs = _coder_job_payload()
    wrong_outputs["EXP-0"]["outputs"] = [
        {"name": "something_else", "source": "stdout", "media_type": "text/plain"}
    ]
    with pytest.raises(CliRefuse, match="not declared") as ei:
        await _run_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_id="st_coder_new",
            study_clock=clock,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=dict(_parse_coder_jobs(wrong_outputs)),
        )
    assert ei.value.code == 3
    events = await _read_events(tmp_path, "st_coder_new")
    assert not [
        e for e in events if e.type == "AttemptStarted" and "/EXP-0#" in str(e.entity_key)
    ], "kill state must have zero attempts on the coder task"

    code, summary = await _continue_phase(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=[],  # coder delivery makes no LLM calls
        study_id="st_coder_new",
        study_clock=_TickingFakeClock(),
        router=_coder_router(),
        coder_backend="local-python",
        coder_jobs=_coder_jobs_decl(["python3", "-c", "print('ok')"]),
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    events = await _read_events(tmp_path, "st_coder_new")
    jobs = [e for e in events if e.type == "JobSubmitted"]
    assert len(jobs) == 1, "the declared job must be dispatched exactly once"
    finalized = [e for e in events if e.type == "StudyFinalized"]
    assert len(finalized) == 1 and finalized[0].payload.get("verdict") == "fulfilled"


@pytest.mark.asyncio
async def test_continue_coder_killed_midjob_refuses_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Kill-mid-job shape (failed job left the coder attempt OPEN, no result):
    recover closes it retryable, but continue typed-refuses (code 3) — the job
    lifecycle is process-local, re-invoking argv would duplicate side effects;
    without the refuse the uncharged infra retries make the study unclosable.
    No second job dispatch may happen."""
    from tests.cli.test_run_study import _coder_plan_json, _coder_router
    from tests.llm.test_client import _ok_body

    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_coder_plan_json(), prompt_tokens=10, completion_tokens=20),
    ]
    with pytest.raises(CliRefuse, match="failed") as ei:
        await _run_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_id="st_coder_dead",
            study_clock=clock,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_coder_jobs_decl(["python3", "-c", "import sys; sys.exit(3)"]),
        )
    assert ei.value.code == 4
    events = await _read_events(tmp_path, "st_coder_dead")
    jobs = [e for e in events if e.type == "JobSubmitted"]
    assert len(jobs) == 1
    exp1_open = [
        e
        for e in events
        if e.type == "AttemptStarted" and not any(
            c.type == "AttemptClosed" and c.entity_key == e.entity_key
            for c in events
        )
    ]
    assert exp1_open, "kill state must leave the coder attempt OPEN"

    with pytest.raises(CliRefuse, match="coder-job attempt") as cont_ei:
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            study_id="st_coder_dead",
            study_clock=_TickingFakeClock(),
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_coder_jobs_decl(["python3", "-c", "print('ok')"]),
        )
    assert cont_ei.value.code == 3
    # No re-dispatch of the declared argv, no finalize.
    events = await _read_events(tmp_path, "st_coder_dead")
    assert len([e for e in events if e.type == "JobSubmitted"]) == 1
    assert not [e for e in events if e.type == "StudyFinalized"]


@pytest.mark.asyncio
async def test_continue_fences_old_epoch_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Fencing proof: continue's takeover bumps the epoch and rotates the nonce;
    the old epoch can neither renew nor append — no split-brain writes."""
    from coscientist.cli import continue_cmd as ccm
    from coscientist.core.events.events import EventEnvelope, make_idem

    rt_old = await open_runtime(tmp_path / "data", "st_fence", clock=clock, owner="owner-old")
    await rt_old.create_study(
        profile="research",
        request="q",
        catalog_version=CATALOG_VERSION,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_fence",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    old_epoch = rt_old.store.epoch
    old_nonce = rt_old.store.owner_nonce
    assert old_epoch >= 1 and old_nonce

    # Predecessor dies without releasing; lease expires (default ttl 30s).
    clock.advance(timedelta(seconds=31))

    # Spy on the takeover: capture the NEW epoch/nonce mid-flight (continue
    # releases its lease cleanly in the finally, so observe before release).
    captured: dict[str, object] = {}
    real_open = ccm.open_runtime

    async def _spy_open(*args: object, **kwargs: object):
        rt = await real_open(*args, **kwargs)  # type: ignore[arg-type]
        captured["epoch"] = rt.store.epoch
        captured["nonce"] = rt.store.owner_nonce
        return rt

    monkeypatch.setattr(ccm, "open_runtime", _spy_open)

    # Continue walks the expiry wait, takes over, then refuses on study state
    # (no accepted plan) — the takeover itself is what this test proves.
    with pytest.raises(CliRefuse, match="no accepted plan"):
        await _continue_phase(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            study_id="st_fence",
            study_clock=clock,
            router=StaticToolRouter(tools=_default_tools()),
            lease_wait_s=60.0,
        )
    assert captured["epoch"] == old_epoch + 1, "takeover must bump the lease epoch"
    assert captured["nonce"] != old_nonce, "takeover must rotate the owner nonce"

    # Old-epoch writer (the "zombie" predecessor): every write path is fenced.
    with pytest.raises(LeaseLost):
        await rt_old.store.renew_lease("owner-old", old_epoch)
    with pytest.raises(LeaseLost, match="epoch fencing rejected write"):
        await rt_old.store.append(
            EventEnvelope(
                study_id="st_fence",
                type="StudyCancelled",
                entity_key="st_fence",
                idem_key=make_idem("StudyCancelled", "st_fence", "zombie"),
                payload={"by": "zombie"},
                ts=clock.now(),
                schema_version=1,
            )
        )
    assert rt_old.store.epoch == old_epoch and rt_old.store.owner_nonce == old_nonce
    await close_runtime(rt_old)


# --- spec re-drive fidelity ----------------------------------------------------


def test_draft_roundtrip_redrivable_for_derive_specs() -> None:
    """derive_plan-shaped specs roundtrip through the draft inverse losslessly."""
    from coscientist.agents.derive import derive_plan

    intent = PlanIntent(
        tasks=(
            TaskIntent(question_indices=(1,),
                goal="overview",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        questions=(QuestionIntent(final_type="artifact", text="q?"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    plan = derive_plan(
        intent,
        catalog_templates=minimal_test_catalog().templates,
        tool_ids=["static:dataset_overview"],
    )
    draft = next(t for t in plan.tasks if t.task_key == "EXP-0")
    spec = _to_task_spec(draft)  # the spec accept_plan journals
    assert spec_redrivable(spec), "journaled derive specs must roundtrip losslessly"
    redraft = draft_from_task_spec(spec)
    assert redraft.task_key == "EXP-0"
    assert redraft.tools_hint == ("static:dataset_overview",)
    assert _to_task_spec(redraft) == spec


def test_spec_with_core_owned_fields_not_redrivable() -> None:
    from coscientist.core.execution.domain import InputSpec

    base = dict(
        task_key="EXP-9",
        title="t",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
    )
    assert not spec_redrivable(TaskSpec(**base, budget_share=0.5))
    assert not spec_redrivable(TaskSpec(**base, inputs=[InputSpec(from_task="EXP-0")]))
    assert not spec_redrivable(TaskSpec(**base, tolerate_failed_dep=True))


def test_spec_with_catalog_enrichment_still_redrivable() -> None:
    """``sandbox_check_limits`` is deterministic TaskCreated enrichment (catalog
    freeze) — it must NOT block a faithful CLI re-drive."""
    from coscientist.agents.derive import derive_plan
    from coscientist.core.execution.domain import FrozenCheckLimits

    intent = PlanIntent(
        tasks=(
            TaskIntent(question_indices=(1,),
                goal="g", route="experiment", key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        questions=(QuestionIntent(final_type="artifact", text="q?"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    plan = derive_plan(
        intent,
        catalog_templates=minimal_test_catalog().templates,
        tool_ids=["static:dataset_overview"],
    )
    spec = _to_task_spec(next(t for t in plan.tasks if t.task_key == "EXP-0"))
    limits = FrozenCheckLimits(
        timeout_s=None, memory_mb=None, max_output_bytes=None, pids_max=None,
        rlimit_nproc=None, reap_timeout_s=None, output_drain_join_s=None,
        allow_network=False, allow_cgroup_degraded=False,
    )
    enriched = spec.model_copy(
        update={"sandbox_check_limits": {"artifact_present": limits, "proposed_check": limits}}
    )
    assert enriched != spec
    assert spec_redrivable(spec)
    assert spec_redrivable(enriched)


# --- argparse surface ----------------------------------------------------------


def test_main_continue_missing_required_args_exits_nonzero() -> None:
    from coscientist.cli.run import main

    with pytest.raises(SystemExit, match="2") as ei:
        main(["continue"])
    assert ei.value.code == 2


def test_main_continue_wires_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``coscientist continue`` main path reaches continue_study with the flags."""
    import json as _json

    from coscientist.cli.run import main

    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    seen: dict[str, object] = {}

    async def _capture(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("coscientist.cli.continue_cmd.continue_study", _capture)
    monkeypatch.setattr(
        "coscientist.cli.run.load_catalog",
        lambda *a, **k: minimal_test_catalog(),
    )

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(minimal_test_catalog().model_dump_json(), encoding="utf-8")
    llm_path = tmp_path / "llm.json"
    from tests.llm.test_client import _config

    llm_path.write_text(_config().model_dump_json(), encoding="utf-8")
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(_json.dumps(_default_tools()), encoding="utf-8")
    model = _RESEARCH_PROFILE.roles["planner"].model
    code = main(
        [
            "continue",
            str(tmp_path / "data"),
            "--study-id",
            "wired",
            "--catalog",
            str(catalog_path),
            "--catalog-version",
            str(CATALOG_VERSION),
            "--profile",
            "research",
            "--tool-catalog-version",
            "2",
            "--owner",
            "cli-test",
            "--llm-config",
            str(llm_path),
            "--planner-model",
            model,
            "--executor-model",
            model,
            "--tz-spec-model",
            model,
            "--tools-json",
            str(tools_path),
            "--lease-ttl-s",
            "60",
            "--lease-wait-s",
            "30",
            "--rng-seed",
            "0",
            "--max-llm-calls",
            "10",
            "--max-tokens",
            "1000",
            "--max-tool-calls",
            "10",
            "--max-ticks",
            "5",
        ]
    )
    assert code == 0
    assert seen["study_id"] == "wired"
    assert seen["lease_wait_s"] == 30.0
    assert seen["catalog_version"] == CATALOG_VERSION
    factory = seen["adapter_factory"]
    assert callable(factory)
