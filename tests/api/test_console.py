"""Study console routes: study list, CAS artifact serving, static SPA mount.

The console is read-only over real journals: the fixture writes a small
events.db (same types/schema_versions as live runs), a CAS file, and asserts
the routes answer from THAT data — nothing invented.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.console import resolve_artifact_file
from coscientist.api.errors import ApiRefuse
from coscientist.core.events.clock import FakeClock, SystemClock
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.payload_schemas import (
    GraphNodeUpsertedPayload,
    StudyCreatedPayload,
    StudyFinalizedPayload,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import Budget, HitlPolicy
from tests.checks.minimal_test_catalog import minimal_test_catalog

_SID = "st_" + "a" * 27
_DIGEST = hashlib.sha256(b"console-fixture-bytes").hexdigest()
_ARTIFACT_ID = f"art_{_DIGEST[:16]}_1add46c6"


class _PollSleep:
    async def __call__(self, _interval: float) -> None:
        await asyncio.sleep(0)


def _event(study_id: str, *, etype: str, entity: str, payload: dict[str, object], schema: int) -> UnstampedEvent:
    return UnstampedEvent(
        study_id=study_id,
        type=etype,  # type: ignore[arg-type]
        entity_key=entity,
        idem_key=f"{etype}:{entity}",
        payload=payload,
        ts="2026-10-26T12:00:00+00:00",
        schema_version=schema,
    )


async def _write_journal(root: Path, sid: str, *, verdict: str | None) -> None:
    """Real-schema journal: payloads are built from the wire models themselves,
    so the store's strict upcast on read accepts them (no invented shapes)."""
    db = root / sid / "events.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(sid, db, clock=FakeClock())
    lease = await store.acquire_lease("fixture")
    assert lease is not None

    created = StudyCreatedPayload(
        study_id=sid,
        catalog_version=4,
        profile="research",
        request="console fixture study",
        hitl_policy=HitlPolicy(mode="headless"),
        deadline="2026-10-26T13:00:00+00:00",
        created_at="2026-10-26T12:00:00+00:00",
        parent_study_id=None,
        budget=Budget(max_llm_calls=10, max_tokens=1000, max_tool_calls=1),
        finalize_reserve_s=30,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        aggregate="count",
        evalue_alpha=0.05,
        max_expansions=0,
        max_attempts=3,
        max_human_expansions=1,
        replan_reserve=0.2,
        max_tasks=12,
        max_depth=3,
        plan_timeout_s=600,
        max_foreach_children=8,
        max_loop_iter=4,
        max_task_budget_share=0.5,
        as_of=None,
        user_input_ids=[],
        max_proposals=4,
        require_hypotheses=False,
    )
    events: list[UnstampedEvent] = [
        _event(
            sid,
            etype="StudyCreated",
            entity=sid,
            payload=created.model_dump(mode="json"),
            schema=16,
        ),
        _event(
            sid,
            etype="GraphNodeUpserted",
            entity=f"{sid}:artifact:{_ARTIFACT_ID}",
            payload=GraphNodeUpsertedPayload(
                node_id=f"{sid}:artifact:{_ARTIFACT_ID}",
                kind="artifact",
                study_id=sid,
                attrs={
                    "artifact_id": _ARTIFACT_ID,
                    "role": "data",
                    "media_type": "text/markdown",
                    "sha256": _DIGEST,
                },
                ref_id=_ARTIFACT_ID,
                parent_id=None,
                created_at="2026-10-26T12:00:01+00:00",
            ).model_dump(mode="json"),
            schema=1,
        ),
    ]
    events.append(_event(
        sid, etype="ArtifactRegistered", entity=_ARTIFACT_ID,
        payload={"artifact_id": _ARTIFACT_ID, "name": "science.md", "sha256": _DIGEST,
                 "role": "report", "media_type": "text/markdown"}, schema=1,
    ))
    if verdict is not None:
        events.append(
            _event(
                sid,
                etype="StudyFinalized",
                entity=sid,
                payload=StudyFinalizedPayload(
                    trigger="deadline",
                    terminal_reason="test",
                    contributing_reasons=[],
                    verdict=verdict,
                    question_dispositions={},
                    report_artifact_id=None,
                    conclusion_ids=[],
                    minimal_report=None,
                    fallback_reason=None,
                    refusal=None,
                    hypothesis_verdicts=None,
                    hypotheses_unavailable=None,
                    hypothesis_report=None,
                    custom_checks=None,
                    observations=None,
                ).model_dump(mode="json"),
                schema=4,
            )
        )
    await store.append_many(events)
    await store.close()


@pytest.fixture()
def console_client(tmp_path: Path) -> Callable[[], TestClient]:
    def _make() -> TestClient:
        return TestClient(
            create_app(
                bind_app_deps(
                    tmp_path,
                    catalog=minimal_test_catalog(),
                    clock=SystemClock(),
                    stream_poll_interval_s=0.01,
                    stream_sleep=_PollSleep(),
                    run_wait_s=1.0,
                )
            )
        )

    return _make


def test_list_studies_verdict_and_open_study(tmp_path: Path, console_client: Callable[[], TestClient]) -> None:
    asyncio.run(_write_journal(tmp_path, _SID, verdict="fulfilled"))
    asyncio.run(_write_journal(tmp_path, "st_" + "b" * 27, verdict=None))
    # a non-sqlite events.db is not a journal — excluded, not a 500
    (tmp_path / "not-a-journal").mkdir()
    (tmp_path / "not-a-journal" / "events.db").write_bytes(b"garbage")
    client = console_client()
    res = client.get("/studies")
    assert res.status_code == 200
    rows = {r["study_id"]: r for r in res.json()}
    assert rows[_SID]["verdict"] == "fulfilled"
    assert rows["st_" + "b" * 27]["verdict"] is None
    assert "not-a-journal" not in rows  # an unreadable file is not a journal


def test_artifact_route_serves_cas_bytes(tmp_path: Path, console_client: Callable[[], TestClient]) -> None:
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    cas = tmp_path / _SID / "arts" / "cas" / _DIGEST
    cas.parent.mkdir(parents=True, exist_ok=True)
    cas.write_bytes(b"console-fixture-bytes")
    client = console_client()
    res = client.get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert res.content == cas.read_bytes()


def test_artifact_route_refuses_unknown_and_malformed(
    tmp_path: Path, console_client: Callable[[], TestClient]
) -> None:
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    client = console_client()
    well_formed_unknown = f"art_{'f' * 16}_ffffffff"
    assert client.get(f"/studies/{_SID}/artifacts/{well_formed_unknown}").status_code == 400
    assert client.get(f"/studies/{_SID}/artifacts/not-an-artifact-id").status_code == 400
    # traversal-shaped segments never match the single-segment route
    assert client.get(f"/studies/{_SID}/artifacts/..%2Fsecrets").status_code in (400, 404)


def test_artifact_missing_cas_bytes_refused(tmp_path: Path, console_client: Callable[[], TestClient]) -> None:
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    client = console_client()
    res = client.get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}")
    assert res.status_code == 400
    assert "artifact_bytes_missing" in res.text


def test_console_static_and_redirect(console_client: Callable[[], TestClient]) -> None:
    client = console_client()
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (301, 302, 307)
    assert res.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "Study Console" in page.text
    assert page.headers["content-type"].startswith("text/html")
    for asset in ("js/app.js", "js/stream.js", "js/model.js", "js/graph.js", "css/console.css"):
        asset_res = client.get(f"/ui/{asset}")
        assert asset_res.status_code == 200, asset


def test_resolve_artifact_file_fail_closed(tmp_path: Path) -> None:
    sid = _SID
    with pytest.raises(ApiRefuse, match="artifact_not_in_journal"):
        resolve_artifact_file(tmp_path, sid, "../escape", events=[])
    with pytest.raises(ApiRefuse, match="artifact_not_in_journal"):
        resolve_artifact_file(tmp_path, sid, _ARTIFACT_ID, events=[])


def test_console_studies_json_shape(tmp_path: Path, console_client: Callable[[], TestClient]) -> None:
    asyncio.run(_write_journal(tmp_path, _SID, verdict="partial"))
    client = console_client()
    rows = client.get("/studies").json()
    assert rows[0].keys() >= {"study_id", "verdict", "updated_at"}
    assert rows[0]["verdict"] == "partial"
    json.dumps(rows)  # wire must stay JSON-serializable


def test_console_ws_streams_cli_format_study(tmp_path: Path, console_client: Callable[[], TestClient]) -> None:
    """Console WS: a CLI-format id (rejected by the strict API gate) streams."""
    sid = "cli-study-freeform"
    asyncio.run(_write_journal(tmp_path, sid, verdict=None))
    client = console_client()
    with client.websocket_connect(f"/console/studies/{sid}/stream?after=0") as ws:
        first = ws.receive_json()
        assert first["type"] == "StudyCreated"
        assert first["study_id"] == sid
    assert client.get(f"/console/studies/{sid}/events?after=0").status_code == 200


def test_registered_report_without_graph_node_is_downloadable(tmp_path, console_client):
    import sqlite3
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    conn = sqlite3.connect(tmp_path / _SID / "events.db")
    conn.execute("DELETE FROM events WHERE type='GraphNodeUpserted'")
    conn.commit()
    conn.close()
    path = tmp_path / _SID / "arts" / "cas" / _DIGEST
    path.parent.mkdir(parents=True)
    path.write_bytes(b"console-fixture-bytes")
    response = console_client().get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == b"console-fixture-bytes"


def test_corrupt_cas_is_not_served(tmp_path, console_client):
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    path = tmp_path / _SID / "arts" / "cas" / _DIGEST
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered scientific result")
    response = console_client().get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}")
    assert response.status_code == 400
    assert response.json()["detail"] == "artifact_digest_mismatch"


def test_unknown_study_does_not_create_database(tmp_path, console_client):
    response = console_client().get("/console/studies/missing/events?after=0")
    assert response.status_code == 404
    assert not (tmp_path / "missing").exists()


def test_symlink_cas_escape_refused(tmp_path, console_client):
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _DIGEST).write_bytes(b"console-fixture-bytes")
    arts = tmp_path / _SID / "arts"
    arts.mkdir()
    (arts / "cas").symlink_to(outside, target_is_directory=True)
    response = console_client().get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}")
    assert response.status_code == 400
    assert response.json()["detail"] == "artifact_path_unsafe"


def test_live_evidence_artifact_without_registration_event(tmp_path, console_client):
    import sqlite3
    asyncio.run(_write_journal(tmp_path, _SID, verdict=None))
    conn = sqlite3.connect(tmp_path / _SID / "events.db")
    conn.execute("DELETE FROM events WHERE type='ArtifactRegistered'")
    conn.commit()
    conn.close()
    path = tmp_path / _SID / "arts" / "cas" / _DIGEST
    path.parent.mkdir(parents=True)
    path.write_bytes(b"console-fixture-bytes")
    assert console_client().get(f"/studies/{_SID}/artifacts/{_ARTIFACT_ID}").content == b"console-fixture-bytes"
