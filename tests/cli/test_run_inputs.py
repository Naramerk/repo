"""CLI ``--task-input``: user study inputs through the ingest gate (typed, fail-closed).

Covers the CLI inputs gap: ``run_study(task_inputs=[...])`` registers files via
the SAME ingest-gate path as ``POST /studies`` (``register_user_input_artifacts``
inside ``create_study``), the journal shows ``ArtifactRegistered(producer=user)``,
attempts bind ``user:<name>`` inputs, and the planner/executor LLM payloads carry
the registered refs (no prompt-path guessing). Refusals are typed.
"""

from __future__ import annotations

import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest

from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import _load_task_inputs, main, run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import HitlPolicy
from coscientist.core.execution.input_limits import UserInput
from coscientist.llm.client import LlmClient
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import _adapter, _cli_plan_out_json, _default_tools, _spec_json
from tests.helpers import close_runtime, open_runtime
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_T = TypeVar("_T")

_RESEARCH_PROFILE = load_profile("research", expected_tool_catalog_version=1)

_CSV_NAME = "reference.csv"
_CSV_BYTES = b"smiles,score\nCCC,0.5\nCCO,0.7\n"


def _client_and_transport(
    bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch
) -> tuple[LlmClient, CassetteTransport]:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
        del timeout_s
        return await awaitable

    transport = CassetteTransport(bodies)
    return (
        LlmClient(
            _config(max_retries=0),
            transport,
            clock=clock,
            sleep=sleep,
            wait_for=wait_for,
            rng=random.Random(0),
        ),
        transport,
    )


def _csv_input(tmp_path: Path) -> UserInput:
    return UserInput(
        name=_CSV_NAME,
        content=_CSV_BYTES,
        media_type="text/csv",
        source_uri=str(tmp_path / _CSV_NAME),
        encoding="raw",
    )


def _task_file(tmp_path: Path) -> Path:
    task = tmp_path / "task.txt"
    task.write_text("Check the reference CSV medians.", encoding="utf-8")
    return task


def _user_message(request: dict[str, object]) -> dict[str, object]:
    """Last user-role message payload of one captured LLM request (JSON object)."""
    messages = request["messages"]
    assert isinstance(messages, list)
    for msg in reversed(messages):
        assert isinstance(msg, dict)
        if msg.get("role") == "user":
            content = msg["content"]
            assert isinstance(content, str)
            loaded = json.loads(content)
            assert isinstance(loaded, dict)
            return loaded
    raise AssertionError("no user message in captured request")


async def _read_events(data_root: Path, study_id: str) -> list[StoredEvent]:
    rt = await open_runtime(data_root, study_id, owner="cli-test-reader")
    try:
        events = await rt.store.read_all()
    finally:
        await close_runtime(rt)
    return [ev for ev in events if isinstance(ev, StoredEvent)]


def _payloads(events: list[StoredEvent], type_: str) -> list[dict[str, object]]:
    rows = [
        {str(k): v for k, v in ev.payload.items()}
        for ev in events
        if ev.type == type_ and isinstance(ev.payload, dict)
    ]
    assert rows, f"no {type_!r} event in journal"
    return rows


async def _run_with_input(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    study_id: str = "inputs_study",
    deadline_s: float = 3600.0,
    finalize_reserve_s: int = 30,
) -> tuple[CassetteTransport, Path]:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(
            content=_exec_out_json(tool="dataset_overview"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    client, transport = _client_and_transport(bodies, monkeypatch=monkeypatch)
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(client, router)
    data_root = tmp_path / "data"
    code = await run_study(
        task_path=_task_file(tmp_path),
        data_root=data_root,
        deadline_s=deadline_s,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-test",
        adapter=adapter,
        out=io.StringIO(),
        study_id=study_id,
        task_inputs=[_csv_input(tmp_path)],
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        finalize_reserve_s=finalize_reserve_s,
        lease_ttl_s=60.0,
        max_llm_calls=100,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        tools=router,
        hitl_policy=HitlPolicy(mode="headless"),
        study_clock=FakeClock(),
    )
    assert code == 0
    return transport, data_root


@pytest.mark.asyncio
async def test_run_with_csv_input_registers_user_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One CSV input → journal shows the ingest-gate ArtifactRegistered(producer=user)."""
    _transport, data_root = await _run_with_input(tmp_path, monkeypatch=monkeypatch)
    events = await _read_events(data_root, "inputs_study")
    reg = _payloads(events, "ArtifactRegistered")[0]
    assert reg["producer"] == "user"
    assert reg["name"] == _CSV_NAME
    assert reg["media_type"] == "text/csv"
    assert reg["source_uri"] == str(tmp_path / _CSV_NAME)
    assert isinstance(reg["artifact_id"], str) and reg["artifact_id"]
    assert isinstance(reg["uri"], str) and reg["uri"]
    created = _payloads(events, "StudyCreated")[0]
    assert created["user_input_ids"] == [reg["artifact_id"]]


@pytest.mark.asyncio
async def test_run_with_csv_input_binds_attempt_and_reaches_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempts bind user:<name>; planner + executor payloads carry the registered refs."""
    transport, data_root = await _run_with_input(tmp_path, monkeypatch=monkeypatch)
    events = await _read_events(data_root, "inputs_study")
    reg = _payloads(events, "ArtifactRegistered")[0]

    exp_starts = [
        row
        for row in _payloads(events, "AttemptStarted")
        if str(row.get("task_id", "")).endswith("/EXP-0")
    ]
    assert exp_starts, "no AttemptStarted for EXP-0"
    bindings = exp_starts[0]["inputs"]
    assert isinstance(bindings, list)
    user_bindings = [
        b for b in bindings if isinstance(b, dict) and b.get("input") == f"user:{_CSV_NAME}"
    ]
    assert user_bindings, f"EXP-0 attempt did not bind user:{_CSV_NAME}: {bindings!r}"
    assert user_bindings[0]["artifact_id"] == reg["artifact_id"]
    assert user_bindings[0]["source"] == "real"

    # Planner request payload (request #2: tz_spec first, planner second).
    planner_payload = _user_message(transport.requests[1])
    planner_inputs = planner_payload["inputs"]
    assert isinstance(planner_inputs, list) and len(planner_inputs) == 1
    assert planner_inputs[0]["name"] == _CSV_NAME
    assert planner_inputs[0]["artifact_id"] == reg["artifact_id"]
    assert planner_inputs[0]["uri"] == reg["uri"]
    # Executor request payload (request #3: first derived task).
    exec_payload = _user_message(transport.requests[2])
    exec_inputs = exec_payload["inputs"]
    assert isinstance(exec_inputs, list) and len(exec_inputs) == 1
    assert exec_inputs[0] == planner_inputs[0]


@pytest.mark.asyncio
async def test_create_refused_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intake refusal (duration <= finalize reserve) → typed CliRefuse, not a start crash."""
    with pytest.raises(CliRefuse, match="create_study refused: duration_too_short") as ei:
        # deadline_s 10 < finalize_reserve_s 30 → core StudyRefused(DURATION_TOO_SHORT).
        await _run_with_input(
            tmp_path,
            monkeypatch=monkeypatch,
            study_id="inputs_refused",
            deadline_s=10.0,
            finalize_reserve_s=30,
        )
    assert ei.value.code == 3


def test_load_task_inputs_maps_files_to_user_inputs(tmp_path: Path) -> None:
    csv = tmp_path / "reference.csv"
    csv.write_bytes(_CSV_BYTES)
    blob = tmp_path / "weights.bin"
    blob.write_bytes(b"\x00\x01")
    inputs = _load_task_inputs([csv, blob])
    assert [i.name for i in inputs] == ["reference.csv", "weights.bin"]
    assert inputs[0].content == _CSV_BYTES
    assert inputs[0].media_type == "text/csv"
    assert inputs[0].source_uri == str(csv)
    assert inputs[0].encoding == "raw"
    assert inputs[1].media_type == "application/octet-stream"


def test_load_task_inputs_missing_file_refuses(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    with pytest.raises(CliRefuse, match="file not found") as ei:
        _load_task_inputs([missing])
    assert ei.value.code == 2
    assert str(missing) in ei.value.detail


def test_load_task_inputs_duplicate_names_refuse(tmp_path: Path) -> None:
    one = tmp_path / "a" / "reference.csv"
    one.parent.mkdir()
    one.write_bytes(b"x\n")
    two = tmp_path / "b" / "reference.csv"
    two.parent.mkdir()
    two.write_bytes(b"y\n")
    with pytest.raises(CliRefuse, match="duplicate artifact name") as ei:
        _load_task_inputs([one, two])
    assert ei.value.code == 2


def test_load_task_inputs_none_is_empty() -> None:
    assert _load_task_inputs(None) == []
    assert _load_task_inputs([]) == []


def test_main_task_input_flag_missing_file_exits_2(tmp_path: Path) -> None:
    """Full argv surface: repeated --task-input reaches the loader; missing file exits 2."""
    task = _task_file(tmp_path)
    tools_json = tmp_path / "tools.json"
    tools_json.write_text(
        json.dumps({"dataset_overview": {"result": {"ok": True}, "args": {}}}), encoding="utf-8"
    )
    llm_config = tmp_path / "llm.json"
    llm_config.write_text("{}", encoding="utf-8")
    good = tmp_path / "reference.csv"
    good.write_bytes(_CSV_BYTES)
    argv = [
        "run",
        str(task),
        "--data-root", str(tmp_path / "data"),
        "--deadline-s", "3600",
        "--catalog-version", str(CATALOG_VERSION),
        "--catalog", "catalog-missing.json",
        "--profile", "research",
        "--tool-catalog-version", "1",
        "--owner", "cli-test",
        "--llm-config", str(llm_config),
        "--planner-model", "m",
        "--executor-model", "m",
        "--tz-spec-model", "m",
        "--tools-json", str(tools_json),
        "--study-id", "flag_inputs",
        "--require-hypotheses", "false",
        "--confirmations-needed", "1",
        "--refutations-needed", "1",
        "--refutation-priority", "false",
        "--supported-needed", "2",
        "--max-expansions", "2",
        "--finalize-reserve-s", "30",
        "--lease-ttl-s", "60",
        "--rng-seed", "0",
        "--max-llm-calls", "10",
        "--max-tokens", "10000",
        "--max-tool-calls", "5",
        "--max-ticks", "5",
        "--hitl-mode", "headless",
        "--task-input", str(good),
        "--task-input", str(tmp_path / "absent.csv"),
    ]
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2
