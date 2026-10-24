"""M1 PR19: input snapshot, user inputs, InputLimits (K10, A3, K1) — review fixes."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.claims.reasons import RefusalKind, TerminalReason
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.events import STUDY_CREATED_SCHEMA_VERSION
from coscientist.core.events.upcasters import upcast, writer_schema_version
from coscientist.core.execution._ingest_gate import (
    mint_user_put_capability,
    put_user,
    release_user_put_capability,
)
from coscientist.core.execution.domain import (
    HitlPolicy,
    Limitation,
    Study,
    TaskSpec,
)
from coscientist.core.execution.input_limits import (
    InputLimits,
    UserInput,
    apply_input_drift,
    check_user_inputs_limits,
    coerce_user_input,
    decode_user_content,
    default_input_limits,
    enforce_request_body_limit,
    inputs_digest_of,
    inspect_zip_limits,
    is_quote_source_producer,
    merge_input_limits,
    snapshot_bindings_at_p2,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime


def _with_user_put(fn):
    """Run ``fn(capability)`` under a live UserPutCapability."""
    cap = mint_user_put_capability()
    try:
        return fn(cap)
    finally:
        release_user_put_capability(cap)



async def _create_with_inputs(rt, *, inputs, **kwargs):
    """create_study with inputs under a minted UserPutCapability."""
    cap = mint_user_put_capability()
    try:
        return await rt.create_study(inputs=inputs, user_put_capability=cap, **kwargs, require_hypotheses=False)
    finally:
        release_user_put_capability(cap)


def _assert_one_study_finalized(events: list) -> object:
    """Exactly one StudyFinalized; report XOR minimal_report; non-empty conclusion_ids."""
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    return fin


def _assert_one_study_refused(
    events: list,
    *,
    kind: str | None = None,
    reason_contains: str | None = None,
) -> object:
    """Exactly one StudyRefused; optional kind / reason substring checks."""
    refused = [e for e in events if e.type == "StudyRefused"]
    assert len(refused) == 1
    ev = refused[0]
    if kind is not None:
        assert ev.payload["kind"] == kind
    if reason_contains is not None:
        assert reason_contains in ev.payload["reason"]
    return ev


def _zip_with_declared_size(*, compress_size: int, file_size: int, name: str = "bomb.bin") -> bytes:
    """Zip whose ZipInfo declares ``file_size`` (may disagree with payload)."""
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(name, b"x" * min(max(compress_size, 1), 64))
        for zi in zf.filelist:
            zi.file_size = file_size
            zi.compress_size = max(1, compress_size)
    return buf2.getvalue()


def test_input_limits_defaults():
    lim = default_input_limits()
    assert lim.max_bytes > 0
    assert lim.max_unpack_ratio > 1


def test_user_producer_not_quote_source():
    """M1: user uploads are task data only — never quote_in_source sources (PR14)."""
    assert not is_quote_source_producer("user")
    assert not is_quote_source_producer("agent")
    assert not is_quote_source_producer("core")
    assert not is_quote_source_producer("sandbox")
    assert is_quote_source_producer("tool")  # PR14 put_fetched / SourceFetched


def test_put_user_is_task_data_not_paper(tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref

    arts = LocalArtifactStore(tmp_path / "arts")

    def _run(cap):
        return put_user(
            arts,
            b"not a paper",
            make_tool_ref("u1", "user_input", "csv"),
            capability=cap,
            name="data.csv",
            media_type="text/csv",
            role="paper",  # ignored — forced data
        )

    art = _with_user_put(_run)
    assert art.producer == "user"
    assert art.role == "data"
    assert not is_quote_source_producer(art.producer)


def test_put_user_denied_without_capability(tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref

    arts = LocalArtifactStore(tmp_path / "arts")
    with pytest.raises(ArtifactRejected, match="user_put_capability_required"):
        put_user(
            arts,
            b"x",
            make_tool_ref("u", "user_input", "x"),
            capability=object(),  # type: ignore[arg-type]
            name="x",
            media_type="text/plain",
        )


def test_store_producer_user_requires_capability(tmp_path):
    """Runtime bypass: raw ``_store(producer=user)`` without token is rejected."""
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref

    arts = LocalArtifactStore(tmp_path / "arts")
    with pytest.raises(ArtifactRejected, match="user_put_capability_required"):
        arts._store(
            b"x",
            make_tool_ref("u", "user_input", "x"),
            name="x",
            media_type="text/plain",
            producer="user",
            role="data",
            tainted=False,
        )


def test_store_forged_capability_object_rejected(tmp_path):
    """Duck-typed ``is_live()`` alone must not pass — exact type + hmac required."""
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref

    class _Forged:
        def is_live(self) -> bool:
            return True

    arts = LocalArtifactStore(tmp_path / "arts")
    with pytest.raises(ArtifactRejected, match="user_put_capability_required"):
        arts._store(
            b"x",
            make_tool_ref("u", "user_input", "x"),
            name="x",
            media_type="text/plain",
            producer="user",
            role="data",
            tainted=False,
            user_put=_Forged(),
        )


def test_issued_registry_not_module_global():
    """sys.modules / setattr cannot inject nonce→token into the put registry."""
    import sys

    import coscientist.core.execution._ingest_gate as gate

    mod = sys.modules[gate.__name__]
    assert not hasattr(mod, "_issued")
    mod._issued = {"dead": "beef" * 8}
    try:
        forged = gate.UserPutCapability(_nonce="dead", _token="beef" * 8)
        assert gate.verify_user_put_capability(forged) is False
        # Live mint still works via closure cells (decoy dict ignored).
        cap = gate.mint_user_put_capability()
        try:
            assert gate.verify_user_put_capability(cap) is True
        finally:
            gate.release_user_put_capability(cap)
    finally:
        del mod._issued


def test_put_user_denied_when_attempt_active(tmp_path):
    """Attempt frame cannot launder via put_user even with a live capability."""
    from coscientist.core.execution._attempt_frame import (
        push_attempt_active,
        reset_attempt_active,
    )
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.taint import push_attempt_taint, reset_attempt_taint

    arts = LocalArtifactStore(tmp_path / "arts")
    t_tok = push_attempt_taint(True)
    a_tok = push_attempt_active(True)
    try:
        cap = mint_user_put_capability()
        try:
            with pytest.raises(ArtifactRejected, match="put_user_requires_ingest_scope"):
                put_user(
                    arts,
                    b"launder",
                    make_tool_ref("u", "user_input", "x"),
                    capability=cap,
                    name="x",
                    media_type="text/plain",
                )
        finally:
            release_user_put_capability(cap)
    finally:
        reset_attempt_active(a_tok)
        reset_attempt_taint(t_tok)


def test_zip_bomb_refused_by_ratio_streaming():
    """Highly compressible payload — streaming ratio refuses (headers ignored)."""
    raw = b"\x00" * 200_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", raw)
    lim = InputLimits(max_bytes=50_000_000, max_unpack_ratio=5.0, max_zip_members=100)
    bad = inspect_zip_limits(buf.getvalue(), lim, name="bomb.zip")
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert "ratio" in bad[1]


def test_zip_understated_header_cannot_bypass():
    """Forged understated file_size cannot bypass limits (stream cutoff or corrupt)."""
    import struct

    raw = b"Z" * 40_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", raw)
    data = bytearray(buf.getvalue())
    # Understate uncompressed size in headers only (compress_size stays real).
    orig = struct.pack("<I", 40_000)
    fake = struct.pack("<I", 8)
    for i in range(len(data) - 3):
        if data[i : i + 4] == orig:
            data[i : i + 4] = fake
    lim = InputLimits(max_bytes=10_000, max_unpack_ratio=1000.0, max_zip_members=100)
    bad = inspect_zip_limits(bytes(data), lim, name="under.zip")
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_INVALID

    # Overstated headers alone must NOT refuse (streaming sees small payload).
    over = _zip_with_declared_size(compress_size=10, file_size=10_000_000)
    lim2 = InputLimits(max_bytes=50_000_000, max_unpack_ratio=10.0, max_zip_members=100)
    assert inspect_zip_limits(over, lim2, name="over.zip") is None


def _corrupt_deflate_zip() -> bytes:
    """Valid zip headers, corrupted DEFLATE payload → zlib.error on read."""
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", b"hello world" * 200)
    data = bytearray(buf.getvalue())
    name_len = struct.unpack_from("<H", data, 26)[0]
    extra_len = struct.unpack_from("<H", data, 28)[0]
    comp_size = struct.unpack_from("<I", data, 18)[0]
    start = 30 + name_len + extra_len
    mid = start + max(1, comp_size // 2)
    data[mid] ^= 0x5A
    data[mid + 1] ^= 0xA5
    return bytes(data)


def test_corrupt_deflate_refused_input_invalid_not_raise():
    """zlib.error on corrupt deflate → INPUT_INVALID; check never raises."""
    from coscientist.core.execution.input_limits import check_user_inputs_limits

    data = _corrupt_deflate_zip()
    bad = check_user_inputs_limits(
        [UserInput(name="c.zip", content=data, media_type="application/zip")],
        InputLimits(),
    )
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_INVALID
    assert "zip read error" in bad[1]


def test_zip_bomb_refused_member_count():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(20):
            zf.writestr(f"f{i}.txt", b"a")
    lim = InputLimits(max_zip_members=5, max_unpack_ratio=1000, max_bytes=10_000_000)
    bad = inspect_zip_limits(buf.getvalue(), lim)
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert "members" in bad[1]


def test_zip_symlink_and_traversal_invalid():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("../etc/passwd")
        zf.writestr(info, b"x")
    lim = InputLimits(max_bytes=10_000, max_unpack_ratio=100, max_zip_members=100)
    bad = inspect_zip_limits(buf.getvalue(), lim)
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "unsafe" in bad[1]

    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 & 0xFFFF) << 16
        zf.writestr(info, b"target")
    bad2 = inspect_zip_limits(buf2.getvalue(), lim)
    assert bad2 is not None and bad2[0] == RefusalKind.INPUT_INVALID
    assert "symlink" in bad2[1]


def test_nested_archive_forbidden():
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("a.txt", b"hi")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("nested.zip", inner.getvalue())
    lim = InputLimits(max_bytes=10_000_000, max_unpack_ratio=100, max_zip_members=100)
    bad = inspect_zip_limits(outer.getvalue(), lim)
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "nested" in bad[1]


def test_corrupt_zip_input_invalid():
    lim = InputLimits(max_bytes=10_000, max_unpack_ratio=100, max_zip_members=100)
    bad = inspect_zip_limits(b"PK\x03\x04not-a-zip", lim)
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID


def test_gzip_and_tar_same_limits():
    # gzip bomb: tiny compressed → large uncompressed
    raw = b"\x00" * 100_000
    gz = gzip.compress(raw)
    lim = InputLimits(max_bytes=50_000_000, max_unpack_ratio=5.0, max_zip_members=100)
    bad = check_user_inputs_limits(
        [UserInput(name="z.gz", content=gz, media_type="application/gzip")],
        lim,
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"abc"))
    bad2 = check_user_inputs_limits(
        [UserInput(name="bad.tar", content=tar_buf.getvalue(), media_type="application/x-tar")],
        lim,
    )
    assert bad2 is not None and bad2[0] == RefusalKind.INPUT_INVALID


def test_oversized_bytes_refused():
    lim = InputLimits(max_bytes=100)
    bad = check_user_inputs_limits(
        [UserInput(name="big", content=b"x" * 200)],
        lim,
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE


def test_pages_rows_from_content_not_claims():
    lim = InputLimits(max_pages=2, max_rows=2)
    # Client claim ignored — content under limit → accept.
    assert (
        check_user_inputs_limits(
            [{"name": "p", "content": "ok", "pages": 9999, "rows": 9999}],
            lim,
        )
        is None
    )
    # Content-derived pages (form-feeds).
    assert (
        check_user_inputs_limits(
            [UserInput(name="p", content=b"a\x0cb\x0cc", media_type="text/plain")],
            lim,
        )[0]
        == RefusalKind.INPUT_TOO_LARGE
    )
    csv = b"h\n" + b"\n".join([b"1,2"] * 10)
    assert (
        check_user_inputs_limits(
            [UserInput(name="r", content=csv, media_type="text/csv")],
            lim,
        )[0]
        == RefusalKind.INPUT_TOO_LARGE
    )


def test_garbage_and_empty_inputs_accepted_under_limits():
    lim = InputLimits(max_bytes=10_000)
    assert check_user_inputs_limits([], lim) is None
    assert check_user_inputs_limits([UserInput(name="e", content=b"")], lim) is None
    garbage = bytes(range(256)) * 4
    assert check_user_inputs_limits(
        [UserInput(name="g", content=garbage, media_type="application/octet-stream")],
        lim,
    ) is None


def test_malformed_input_refused_input_invalid():
    class Weird:
        pass

    bad = check_user_inputs_limits([Weird()], InputLimits())
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_INVALID
    assert bad[1].startswith("malformed_input:")


def test_unicode_user_input_under_limits():
    lim = InputLimits(max_bytes=10_000)
    text = "仮説データ — ข้อมูลไทย — 中文"
    assert (
        check_user_inputs_limits(
            [UserInput(name="仮説.csv", content=text, encoding="utf-8")],
            lim,
        )
        is None
    )


def test_base64_decode_validate_true_no_fallback():
    import base64
    import binascii

    raw = base64.b64encode(b"hello").decode()
    inp = coerce_user_input(
        {"name": "b64", "content": raw, "encoding": "base64"}
    )
    assert decode_user_content(inp) == b"hello"
    with pytest.raises(binascii.Error):
        decode_user_content(
            UserInput(name="bad", content="!!!!", encoding="base64")
        )


def test_merge_input_limits_client_may_only_lower():
    profile = InputLimits(max_bytes=1000, max_pages=10)
    ok = merge_input_limits(profile, {"max_bytes": 500, "max_pages": 10})
    assert isinstance(ok, InputLimits) and ok.max_bytes == 500
    assert ok.max_pages == 10
    bad = merge_input_limits(profile, {"max_bytes": 10_000})
    assert isinstance(bad, tuple) and bad[0] == RefusalKind.INPUT_INVALID
    for forbidden in (0, -1, True, False):
        bad_f = merge_input_limits(profile, {"max_bytes": forbidden})
        assert isinstance(bad_f, tuple) and bad_f[0] == RefusalKind.INPUT_INVALID
    # Profile may set None (= off); client cannot.
    profile_off = InputLimits(max_bytes=None, max_pages=10)
    assert profile_off.max_bytes is None
    bad_none = merge_input_limits(profile_off, {"max_bytes": None})
    assert isinstance(bad_none, tuple) and bad_none[0] == RefusalKind.INPUT_INVALID


def test_merge_rejects_nan_inf_unpack_ratio():
    from pydantic import ValidationError

    profile = InputLimits(max_unpack_ratio=100.0)
    for bad_v in (float("nan"), float("inf"), float("-inf")):
        bad = merge_input_limits(profile, {"max_unpack_ratio": bad_v})
        assert isinstance(bad, tuple) and bad[0] == RefusalKind.INPUT_INVALID
    with pytest.raises(ValidationError):
        InputLimits(max_unpack_ratio=float("nan"))


def test_merge_rejects_unknown_limit_keys():
    profile = InputLimits(max_bytes=1000)
    bad = merge_input_limits(profile, {"max_bytes": 500, "max_loop_iter": 8})
    assert isinstance(bad, tuple) and bad[0] == RefusalKind.INPUT_INVALID
    assert "unknown" in bad[1]


def test_limits_is_not_none_not_truthiness():
    """Zero must not disable via truthiness — client 0 rejected; profile None = off."""
    from coscientist.core.execution.input_limits import check_user_inputs_limits

    # Profile None = unlimited for that field.
    lim = InputLimits(max_bytes=None, max_pages=None, max_rows=None, max_wire_bytes=None)
    assert (
        check_user_inputs_limits([UserInput(name="b", content=b"x" * 1000)], lim)
        is None
    )


def test_inputs_digest_stable_sorted():
    from coscientist.core.execution.domain import InputBinding

    a = InputBinding(input="b", artifact_id="1", source="real", digest="aa")
    b = InputBinding(input="a", artifact_id="2", source="real", digest="bb")
    d1 = inputs_digest_of([a, b])
    d2 = inputs_digest_of([b, a])
    assert d1 == d2
    assert len(d1) == 64


def test_input_drift_forces_partial():
    lims: list[Limitation] = []
    prov, lims2 = apply_input_drift(
        provenance="real",
        limitations=lims,
        inputs_digest="aaa",
        previous_inputs_digest="bbb",
    )
    assert prov == "partial"
    drift = [x for x in lims2 if x.text == "input_drift"]
    assert len(drift) == 1


def test_legacy_null_digest_drift_unknown_not_partial():
    """v7 journals with digest=None must not flip provenance to partial."""
    prev = [
        {"input": "x", "artifact_id": "a", "source": "real", "digest": None},
    ]
    prov, lims = apply_input_drift(
        provenance="real",
        limitations=[],
        inputs_digest="newdigest",
        previous_inputs_digest="olddigest",
        previous_bindings=prev,
    )
    assert prov == "real"
    unknown = [x for x in lims if x.text == "input_drift_unknown"]
    assert len(unknown) == 1
    assert len([x for x in lims if x.text == "input_drift"]) == 0


def test_snapshot_bindings_uses_cas_sha(tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.domain import InputBinding

    arts = LocalArtifactStore(tmp_path / "arts")
    art = _with_user_put(
        lambda cap: put_user(
            arts,
            b"payload-bytes",
            make_tool_ref("c1", "user_input", "n"),
            capability=cap,
            name="n",
            media_type="text/plain",
        )
    )
    b = InputBinding(
        input="n",
        artifact_id=art.artifact_id,
        source="real",
        digest="old-meta",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    out, lims = snapshot_bindings_at_p2([b], now=now, get_meta=arts.get_meta)
    assert lims == []
    assert out[0].digest == art.sha256
    assert out[0].retrieved_at == now
    assert out[0].digest_version == 1
    # CAS file:// must not overwrite client source_uri (absent here → stays None).
    assert out[0].source_uri is None


def test_snapshot_get_meta_errors_propagate():
    from coscientist.core.execution.domain import InputBinding

    b = InputBinding(input="n", artifact_id="art_x", source="real")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def boom(_aid: str):
        raise RuntimeError("meta boom")

    with pytest.raises(RuntimeError, match="meta boom"):
        snapshot_bindings_at_p2([b], now=now, get_meta=boom)


def test_snapshot_missing_meta_yields_limitation():
    from coscientist.core.execution.domain import InputBinding

    b = InputBinding(
        input="n",
        artifact_id="art_missing",
        source="real",
        digest="meta-only",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    out, lims = snapshot_bindings_at_p2([b], now=now, get_meta=lambda _aid: None)
    assert out[0].digest == "meta-only"
    assert len(lims) == 1
    assert lims[0].text == "input_digest_unavailable"


def test_wire_body_limit_before_decode():
    bad = enforce_request_body_limit(b"x" * 100, max_wire_bytes=50)
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert enforce_request_body_limit(b"ok", max_wire_bytes=50) is None


@pytest.mark.asyncio
async def test_intake_zip_bomb_refused():
    from coscientist.core.plan.intake import IntakeRefuse, intake

    raw = b"\x00" * 200_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", raw)
    r = await intake(
        "study please",
        [UserInput(name="bomb.zip", content=buf.getvalue(), media_type="application/zip")],
        "research",
        None,
        input_limits=InputLimits(max_unpack_ratio=5.0, max_bytes=50_000_000),
    )
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.INPUT_TOO_LARGE


@pytest.mark.asyncio
async def test_zip_bomb_refused_ends_in_study_finalized(tmp_path, clock, runtime_cls):
    """Adversarial zip → StudyRefused + StudyFinalized (never crash)."""
    sid = "st_zip_bomb"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    raw = b"\x00" * 200_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", raw)
    await _create_with_inputs(rt, 
        profile="research",
        request="analyse archive",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[
            UserInput(
                name="bomb.zip", content=buf.getvalue(), media_type="application/zip"
            )
        ],
        input_limits=InputLimits(max_unpack_ratio=5.0, max_bytes=50_000_000),
    )
    events = await rt.store.read_all()
    _assert_one_study_refused(events, kind=RefusalKind.INPUT_TOO_LARGE.value)
    fin = _assert_one_study_finalized(events)
    assert fin.payload["terminal_reason"] == TerminalReason.REFUSED.value
    assert fin.payload["trigger"] == "intake"
    assert rt._finalized and rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_zip_bomb_refuse_survives_finalize_crash(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Kill after StudyRefused (before finalize) → reopen still closes honestly."""
    sid = "st_zip_crash"

    async def boom(*_a, **_k):
        raise RuntimeError("inject:finalize_crash")

    monkeypatch.setattr("coscientist.core.finalize.finalize.finalize", boom)
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    raw = b"\x00" * 200_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", raw)
    with pytest.raises(RuntimeError, match="inject:finalize_crash"):
        await _create_with_inputs(rt, 
            profile="research",
            request="analyse archive",
            catalog_version=3,
            deadline=clock.now() + timedelta(seconds=120),
            sid=sid,
            finalize_reserve_s=5,
            inputs=[
                UserInput(
                    name="bomb.zip",
                    content=buf.getvalue(),
                    media_type="application/zip",
                )
            ],
            input_limits=InputLimits(max_unpack_ratio=5.0, max_bytes=50_000_000),
        )
    events_crash = await rt.store.read_all()
    types = {e.type for e in events_crash}
    assert "StudyCreated" in types
    _assert_one_study_refused(events_crash)
    assert "StudyFinalized" not in types
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2._intake_refusal is not None
    assert rt2._intake_refusal.kind == RefusalKind.INPUT_TOO_LARGE
    await rt2.tick()
    events2 = await rt2.store.read_all()
    fin = _assert_one_study_finalized(events2)
    assert fin.payload["trigger"] == "intake"
    assert fin.payload["terminal_reason"] == TerminalReason.REFUSED.value
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_client_raising_limits_refuses_input_invalid(
    tmp_path, clock, runtime_cls
):
    sid = "st_lim_raise"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="raise limits",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=90),
        sid=sid,
        finalize_reserve_s=5,
        input_limits=InputLimits(max_bytes=1000),
        client_input_limits={"max_bytes": 10_000_000},
        require_hypotheses=False,
    )
    events = await rt.store.read_all()
    _assert_one_study_refused(events, kind=RefusalKind.INPUT_INVALID.value)
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_user_inputs_registered_before_spec(tmp_path, clock, runtime_cls):
    sid = "st_user_in"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await _create_with_inputs(rt, 
        profile="research",
        request="use my csv",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        as_of=datetime(2020, 1, 1, tzinfo=UTC),
        inputs=[
            UserInput(
                name="data.csv",
                content=b"a,b\n1,2\n",
                media_type="text/csv",
                source_uri="s3://bucket/data.csv",
            )
        ],
    )
    assert rt.study is not None
    assert rt.study.as_of is not None
    assert len(rt.study.user_input_ids) == 1
    aid = rt.study.user_input_ids[0]
    events = await rt.store.read_all()
    types = [e.type for e in events]
    assert "ArtifactRegistered" in types
    assert types.index("ArtifactRegistered") < types.index("TaskCreated")
    regs = [e for e in events if e.type == "ArtifactRegistered"]
    assert len(regs) == 1
    reg = regs[0]
    assert reg.entity_key == aid
    assert reg.payload.get("producer") == "user"
    assert reg.payload.get("role") == "data"
    # CAS uri retained; client URI only in source_uri.
    uri = reg.payload.get("uri")
    assert isinstance(uri, str) and uri.startswith("file://")
    assert reg.payload.get("source_uri") == "s3://bucket/data.csv"
    specs = [t for t in rt.tasks.values() if t.spec.task_key == "SPEC"]
    assert len(specs) == 1
    meta = rt.artifacts.get_meta(aid)
    assert meta is not None and meta.producer == "user"
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2.study is not None
    assert rt2.study.user_input_ids == [aid]
    assert rt2.study.as_of is not None
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_user_input_ids_bound_into_attempt(tmp_path, clock, runtime_cls):
    sid = "st_bind_user"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await _create_with_inputs(rt, 
        profile="research",
        request="bind me",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[UserInput(name="data.csv", content=b"a,b\n1,2\n", media_type="text/csv")],
    )
    uid = rt.study.user_input_ids[0]
    aid = await rt.start_attempt("SPEC")
    started = next(
        e
        for e in await rt.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid
    )
    inputs = started.payload.get("inputs")
    assert isinstance(inputs, list)
    bound = [
        row
        for row in inputs
        if isinstance(row, dict) and row.get("artifact_id") == uid
    ]
    assert len(bound) == 1
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_input_digest_recorded(tmp_path, clock, runtime_cls):
    sid = "st_in_dig"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            )
        ],
        questions=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E1")
    started = next(
        e
        for e in await rt.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid
    )
    # #23 v8 inputs_digest; PR20 v9 role; R16a v10 provenance; v11 Unrecorded.
    assert started.schema_version == 11
    assert started.payload.get("inputs_digest")
    assert isinstance(started.payload["inputs_digest"], str)
    assert len(started.payload["inputs_digest"]) == 64
    assert started.payload.get("attempt_role") == "agent"
    assert started.payload.get("provenance") == "real"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_input_drift_limitation(tmp_path, clock, runtime_cls):
    """Retry after CAS content change on bound artifact → input_drift + partial."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import InputSpec, OutputSpec, TaskOutcome
    from tests.helpers import attach_executor, record_success

    sid = "st_drift"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    _ex, arts = attach_executor(rt, tmp_path)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P1",
                title="p",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json")
                ],
            ),
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["P1"],
                inputs=[InputSpec(from_task="P1", output="main", mode="forbid")],
            ),
        ],
        questions=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid_p = await rt.start_attempt("P1")
    await record_success(rt, aid_p, arts)
    assert rt.tasks[f"{sid}/P1"].outcome == TaskOutcome.SUCCESS
    aid1 = await rt.start_attempt("E1")
    dig1 = rt.attempts[aid1]["inputs_digest"]
    assert dig1
    await rt.fail_attempt(aid1, retryable=True)
    new_art = arts.put_core(
        b'{"count": 999, "drift": true}',
        make_tool_ref(f"{aid_p}:v2", "core", "main"),
        name="main",
        media_type="application/json",
        role="data",
    )
    rt.dep_outputs["P1"] = {"main": new_art.artifact_id}
    aid2 = await rt.start_attempt("E1")
    started2 = next(
        e
        for e in await rt.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid2
    )
    dig2 = started2.payload["inputs_digest"]
    assert dig2 != dig1
    assert started2.payload["provenance"] == "partial"
    lims = started2.payload.get("limitations")
    assert isinstance(lims, list)
    drift = [
        x
        for x in lims
        if (x.get("text") if isinstance(x, dict) else getattr(x, "text", None))
        == "input_drift"
    ]
    assert len(drift) == 1
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_v7_journal_null_digest_no_false_drift(tmp_path, clock, runtime_cls):
    """Upcasted v7 AttemptStarted (digest=None) → drift unknown, not partial."""
    sid = "st_v7_drift"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            )
        ],
        questions=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    # Seed a synthetic prior CLOSED attempt as if replayed from v7 (null digests).
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    fake_aid = f"{task.task_id}/a1"
    v, payload = upcast(
        "AttemptStarted",
        7,
        {
            "attempt_id": fake_aid,
            "n": 1,
            "dispatch_id": f"dispatch:{fake_aid}",
            "task_id": task.task_id,
            "study_id": sid,
            "provenance": "real",
            "closed": True,
            "close_reason": "failed",
            "inputs": [{"input": "x", "artifact_id": "art_old", "source": "real"}],
            "tainted_context": False,
            "inputs_digest": "deadbeef" * 8,
        },
    )
    # #23 v8; PR20 v9; R16a v10 provenance; v11 Unrecorded.
    assert v == 11
    assert payload["inputs"][0].get("digest") is None
    assert payload.get("attempt_role") == "agent"
    assert payload.get("provenance") == "real"
    rt.attempts[fake_aid] = payload
    task.attempts.append(fake_aid)

    aid2 = await rt.start_attempt("E1")
    started2 = next(
        e
        for e in await rt.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid2
    )
    assert started2.payload["provenance"] == "real"
    lims = started2.payload.get("limitations") or []
    texts = [
        x.get("text") if isinstance(x, dict) else getattr(x, "text", None) for x in lims
    ]
    assert "input_drift" not in texts
    assert "input_drift_unknown" in texts
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_adversarial_empty_garbage_still_finalizes(tmp_path, clock, runtime_cls):
    """Empty + garbage inputs accepted → study still closes honestly."""
    sid = "st_adv"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await _create_with_inputs(rt, 
        profile="research",
        request="anything",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=90),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[
            UserInput(name="empty", content=b""),
            UserInput(name="garbage", content=b"\x00\xff\xfe" * 100),
        ],
    )
    for tid in list(rt.tasks):
        t = rt.tasks[tid]
        if t.state.value != "DONE":
            await rt.cancel_task(tid, reason="cancelled")
    await rt.tick()
    events = await rt.store.read_all()
    _assert_one_study_finalized(events)
    assert rt._finalized and rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_as_of_appears_in_report(tmp_path, clock, runtime_cls):
    sid = "st_asof"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    as_of = datetime(2019, 6, 15, tzinfo=UTC)
    await rt.create_study(
        profile="research",
        request="time sensitive",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=90),
        sid=sid,
        finalize_reserve_s=5,
        as_of=as_of,
        require_hypotheses=False,
    )
    for tid in list(rt.tasks):
        if rt.tasks[tid].state.value != "DONE":
            await rt.cancel_task(tid, reason="cancelled")
    await rt.tick()
    events = await rt.store.read_all()
    fin = _assert_one_study_finalized(events)
    rid = fin.payload.get("report_artifact_id")
    assert rid is not None
    assert rt.artifacts is not None
    md = rt.artifacts.read(rid).decode("utf-8")
    assert "as_of" in md
    assert "2019-06-15" in md
    await close_runtime(rt)


def test_upcast_study_created_v9_to_v11():
    """#24 v10 setdefault max_loop_iter; #23 v11 as_of/ids; PR20 v12 max_proposals."""
    v, p = upcast(
        "StudyCreated", 9,
        {
            "catalog_version": 3, "study_id": "s", "request": "q",
            "require_hypotheses": False,
        },
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert p.get("max_loop_iter") == 8
    assert p.get("as_of") is None
    assert p.get("user_input_ids") == []
    assert p.get("max_proposals") == 3


def test_upcast_study_created_v10_to_v11_keeps_max_loop_iter():
    """Custom max_loop_iter (≠ default 8) must survive v10→v11→v12."""
    v, p = upcast(
        "StudyCreated",
        10,
        {
            "study_id": "s", "request": "q", "max_loop_iter": 2, "catalog_version": 3,
            "require_hypotheses": False,
        },
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert p["max_loop_iter"] == 2
    assert p.get("as_of") is None
    assert p.get("user_input_ids") == []
    assert p.get("max_proposals") == 3


@pytest.mark.asyncio
async def test_max_loop_iter_survives_replay(tmp_path, clock, runtime_cls):
    """Writer v12 records max_loop_iter=2; reopen must not raise the cap to 8."""
    sid = "st_loop_iter_2"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        max_loop_iter=2,
        require_hypotheses=False,
    )
    assert rt.study is not None
    assert rt.study.max_loop_iter == 2
    created = next(e for e in await rt.store.read_all() if e.type == "StudyCreated")
    assert created.schema_version == STUDY_CREATED_SCHEMA_VERSION
    assert created.payload.get("max_loop_iter") == 2
    assert created.payload.get("max_proposals") == 3
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2.study is not None
    assert rt2.study.max_loop_iter == 2
    await close_runtime(rt2)


def test_upcast_attempt_started_v7_to_v8():
    """#23 v8; PR20 v9; R16a v10 provenance; v11 AttemptUnrecorded."""
    v, p = upcast(
        "AttemptStarted",
        7,
        {
            "attempt_id": "a",
            "inputs": [{"input": "x", "artifact_id": "", "source": "real"}],
            "tainted_context": False,
            "provenance": "real",
        },
    )
    assert v == 10
    assert "inputs_digest" in p
    assert p.get("attempt_role") == "agent"
    assert p.get("provenance") == "real"
    assert p["inputs"][0].get("digest") is None
    assert p["inputs"][0].get("digest_version") is None
    assert p["inputs"][0].get("retrieved_at") is None
    assert p["inputs"][0].get("source_uri") is None


@pytest.mark.asyncio
async def test_oversized_input_refused_via_create_study(tmp_path, clock, runtime_cls):
    sid = "st_big"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await _create_with_inputs(rt, 
        profile="research",
        request="too big",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=90),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[UserInput(name="huge.bin", content=b"y" * 1000)],
        input_limits=InputLimits(max_bytes=100),
    )
    events = await rt.store.read_all()
    _assert_one_study_refused(events)
    _assert_one_study_finalized(events)
    spec_created = [
        e
        for e in events
        if e.type == "TaskCreated"
        and isinstance(e.payload.get("spec"), dict)
        and e.payload["spec"].get("task_key") == "SPEC"
    ]
    assert len(spec_created) == 0
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_missing_artifact_store_refuses_user_inputs(
    tmp_path, clock, runtime_cls
):
    """Non-empty inputs + no put_user store → StudyRefused (never silent [])."""
    sid = "st_no_arts"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    rt.artifacts = None
    await _create_with_inputs(rt, 
        profile="research",
        request="need data",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=90),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[UserInput(name="d.bin", content=b"abc")],
    )
    events = await rt.store.read_all()
    _assert_one_study_refused(
        events,
        kind=RefusalKind.INPUT_INVALID.value,
        reason_contains="artifact_store_unavailable",
    )
    _assert_one_study_finalized(events)
    assert len([e for e in events if e.type == "ArtifactRegistered"]) == 0
    await close_runtime(rt)


def test_api_parse_rejects_raised_limits_as_input_invalid():
    from coscientist.api.studies import parse_create_study_body

    profile = InputLimits(max_bytes=1000, max_wire_bytes=10_000)
    parsed = parse_create_study_body(
        {"request": "q", "catalog_version": 3, "input_limits": {"max_bytes": 500}, "require_hypotheses": False},
        profile_limits=profile,
    )
    assert parsed[0] is not None
    body, lim = parsed
    assert lim.max_bytes == 500
    none, bad = parse_create_study_body(
        {"request": "q", "catalog_version": 3, "input_limits": {"max_bytes": 10**12}, "require_hypotheses": False},
        profile_limits=profile,
    )
    assert none is None and bad[0] == RefusalKind.INPUT_INVALID


def test_api_wire_body_dos_before_decode():
    from coscientist.api.studies import parse_create_study_body

    profile = InputLimits(max_bytes=1000, max_wire_bytes=64)
    huge = json.dumps({"request": "x" * 200}).encode()
    assert len(huge) > 64
    none, bad = parse_create_study_body(huge, profile_limits=profile)
    assert none is None and bad[0] == RefusalKind.INPUT_TOO_LARGE


@pytest.mark.asyncio
async def test_api_validation_error_becomes_study_finalized(tmp_path, catalog):
    """ValidationError on body → Refuse(INPUT_INVALID) → StudyFinalized, never 500."""
    from coscientist.api.studies import post_studies

    raw = json.dumps(
        {
            "request": "q",
            "catalog_version": 3,
            "input_limits": {"max_bytes": -5}, "require_hypotheses": False
        }
    ).encode()
    out = await post_studies(
        tmp_path,
        raw,
        catalog=catalog,
        profile_input_limits=InputLimits(max_bytes=1000, max_wire_bytes=50_000),
    )
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    refused = _assert_one_study_refused(events, kind=RefusalKind.INPUT_INVALID.value)
    assert refused.payload.get("catalog_version") == 3
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_api_validation_error_without_catalog_version_still_finalized(
    tmp_path, catalog
):
    """No client catalog_version: keep original refuse reason; StudyRefused.cv=null."""
    from coscientist.api.studies import post_studies
    from coscientist.core.events.errors import Conflict

    raw = json.dumps(
        {
            "request": "q",
            "input_limits": {"max_bytes": -5},
        }
    ).encode()
    try:
        out = await post_studies(
            tmp_path,
            raw,
            catalog=catalog,
            profile_input_limits=InputLimits(max_bytes=1000, max_wire_bytes=50_000),
        )
    except Conflict as e:
        raise AssertionError(f"refuse path must not raise Conflict: {e}") from e
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    # Original parse reason (missing StrictInt → body ValidationError), not overwritten.
    refused = _assert_one_study_refused(
        events,
        kind=RefusalKind.INPUT_INVALID.value,
        reason_contains="body:ValidationError",
    )
    assert refused.payload.get("catalog_version") is None
    assert "catalog_version_required" not in refused.payload["reason"]
    assert not any(e.type == "StudyCreated" for e in events)
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_version", ["3", 3.0, True])
async def test_api_refuse_path_bad_catalog_version_type_finalized(
    tmp_path, catalog, catalog_version
):
    """Wrong catalog_version types → StudyFinalized; original reason kept; cv null."""
    from coscientist.api.studies import post_studies
    from coscientist.core.events.errors import Conflict

    raw = json.dumps(
        {
            "request": "q",
            "catalog_version": catalog_version,
            "input_limits": {"max_bytes": -5}, "require_hypotheses": False
        }
    ).encode()
    try:
        out = await post_studies(
            tmp_path,
            raw,
            catalog=catalog,
            profile_input_limits=InputLimits(max_bytes=1000, max_wire_bytes=50_000),
        )
    except Conflict as e:
        raise AssertionError(f"refuse path must not raise Conflict: {e}") from e
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    refused = _assert_one_study_refused(
        events,
        kind=RefusalKind.INPUT_INVALID.value,
        reason_contains="body:ValidationError",
    )
    assert refused.payload.get("catalog_version") is None
    assert "catalog_version_must_be_strict_int" not in refused.payload["reason"]
    assert not any(e.type == "StudyCreated" for e in events)
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_api_refuse_keeps_input_too_large_over_catalog_version_reason(
    tmp_path, catalog
):
    """INPUT_TOO_LARGE must not be overwritten by catalog_version_* refuse text."""
    from coscientist.api.studies import post_studies

    # Oversized wire body (and no parseable catalog_version).
    raw = json.dumps({"request": "x" * 200, "require_hypotheses": False}).encode()
    out = await post_studies(
        tmp_path,
        raw,
        catalog=catalog,
        profile_input_limits=InputLimits(max_bytes=1000, max_wire_bytes=64),
    )
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    refused = _assert_one_study_refused(
        events, kind=RefusalKind.INPUT_TOO_LARGE.value
    )
    assert refused.payload.get("catalog_version") is None
    assert "catalog_version_" not in refused.payload["reason"]
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_create_study_none_catalog_version_is_catalog_error(tmp_path, clock):
    """create_study(catalog_version=None) fails typed — Study requires StrictInt."""
    from coscientist.core.events.errors import CatalogError

    rt = await open_runtime(tmp_path, "st_cv_none", clock=clock, owner="t1")
    with pytest.raises(CatalogError, match="catalog_version_must_be_strict_int"):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=None,  # type: ignore[arg-type]
            sid="st_cv_none",
            finalize_reserve_s=5,
            require_hypotheses=False,
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_studies_idempotency_digest_is_full_body_conflict(
    tmp_path, catalog
):
    """Two different invalid bodies with the same Idempotency-Key → Conflict."""
    from coscientist.api.studies import post_studies
    from coscientist.core.events.errors import Conflict

    key = "idem-invalid-full-body"
    limits = InputLimits(max_bytes=1000, max_wire_bytes=50_000)
    a = json.dumps(
        {"request": "a", "catalog_version": "3", "input_limits": {"max_bytes": -1}, "require_hypotheses": False}
    ).encode()
    b = json.dumps(
        {"request": "b", "catalog_version": "3", "input_limits": {"max_bytes": -1}, "require_hypotheses": False}
    ).encode()
    out = await post_studies(
        tmp_path, a, idempotency_key=key, catalog=catalog, profile_input_limits=limits
    )
    assert out["study_id"]
    with pytest.raises(Conflict, match="different POST /studies body"):
        await post_studies(
            tmp_path,
            b,
            idempotency_key=key,
            catalog=catalog,
            profile_input_limits=limits,
        )


@pytest.mark.asyncio
async def test_post_studies_conflict_requires_same_request_study_created(
    tmp_path, catalog, monkeypatch
):
    """except Conflict is success only when StudyCreated matches this request."""
    from coscientist.api.studies import post_studies
    from coscientist.core.events.errors import Conflict

    raw = json.dumps(
        {
            "request": "same-req",
            "catalog_version": 3,
            "profile": "research",
            "input_limits": {"max_bytes": -5}, "require_hypotheses": False
        }
    ).encode()
    limits = InputLimits(max_bytes=1000, max_wire_bytes=50_000)

    async def _boom(*_a, **_k):
        raise Conflict("peer payload clock")

    monkeypatch.setattr(
        "coscientist.api.runtime.StudyRuntime.create_study",
        _boom,
        raising=True,
    )
    # No StudyCreated yet → Conflict must propagate (not treated as success).
    with pytest.raises(Conflict, match="peer payload clock"):
        await post_studies(
            tmp_path,
            raw,
            catalog=catalog,
            profile_input_limits=limits,
            idempotency_key="conflict-same-req",
        )

    # Seed a StudyCreated for a *different* request under the same study_id.
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.events.ids import study_id_from_idempotency_key

    key = "conflict-other-req"
    sid = study_id_from_idempotency_key(key)
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    await rt.writer.append(EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload=Study(
                    study_id=sid,
                    profile="research",
                    request="other-request",
                    catalog_version=3,
                    finalize_reserve_s=30,
                    hitl_policy=HitlPolicy(),
                    deadline=datetime(2026, 1, 2, tzinfo=UTC),
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    require_hypotheses=False,
                ).model_dump(mode="json"),
                ts=rt.clock.now(),
                schema_version=writer_schema_version("StudyCreated"),
            ))
    await close_runtime(rt)

    with pytest.raises(Conflict, match="peer payload clock"):
        await post_studies(
            tmp_path,
            raw,
            catalog=catalog,
            profile_input_limits=limits,
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_refuse_path_preserves_client_profile_and_finalize_reserve(
    tmp_path, catalog
):
    """Refuse path builds via model_validate — client profile/reserve kept, no construct."""
    from coscientist.api.studies import post_studies

    raw = json.dumps(
        {
            "request": "q",
            "catalog_version": 3,
            "profile": "audit",
            "finalize_reserve_s": 99,
            "input_limits": {"max_bytes": -5}, "require_hypotheses": False
        }
    ).encode()
    out = await post_studies(
        tmp_path,
        raw,
        catalog=catalog,
        profile_input_limits=InputLimits(max_bytes=1000, max_wire_bytes=50_000),
    )
    rt = await open_runtime(tmp_path, out["study_id"], catalog=catalog)
    events = await rt.store.read_all()
    created = [e for e in events if e.type == "StudyCreated"]
    assert len(created) == 1
    assert created[0].payload["profile"] == "audit"
    assert created[0].payload["finalize_reserve_s"] == 99
    _assert_one_study_refused(events, kind=RefusalKind.INPUT_INVALID.value)
    await close_runtime(rt)


def test_artifact_producer_includes_tool_and_user():
    from typing import get_args

    from coscientist.core.execution.domain import ArtifactProducer

    assert set(get_args(ArtifactProducer)) == {
        "agent",
        "core",
        "sandbox",
        "tool",
        "user",
    }


def test_tgz_symlink_and_char_device_invalid():
    """tar.gz with symlink ../../etc/x or char device → INPUT_INVALID."""
    from coscientist.core.execution.archive_limits import inspect_archive
    from coscientist.core.execution.input_limits import check_user_inputs_limits

    # Symlink member via tar.gz
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="../../etc/x")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    data = tar_buf.getvalue()
    lim = InputLimits(max_bytes=10_000_000, max_unpack_ratio=100, max_zip_members=100)
    bad = inspect_archive(
        data,
        max_bytes=lim.max_bytes,
        max_unpack_ratio=lim.max_unpack_ratio,
        max_zip_members=lim.max_zip_members,
        name="evil.tgz",
        media_type="application/gzip",
    )
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_INVALID

    # Char device
    tar_buf2 = io.BytesIO()
    with tarfile.open(fileobj=tar_buf2, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="dev/null")
        info.type = tarfile.CHRTYPE
        tf.addfile(info)
    bad2 = check_user_inputs_limits(
        [
            UserInput(
                name="dev.tgz",
                content=tar_buf2.getvalue(),
                media_type="application/gzip",
            )
        ],
        lim,
    )
    assert bad2 is not None
    assert bad2[0] == RefusalKind.INPUT_INVALID
    assert "special" in bad2[1]


def test_bz2_bomb_refused():
    import bz2

    raw = b"\x00" * 200_000
    data = bz2.compress(raw)
    lim = InputLimits(max_bytes=50_000_000, max_unpack_ratio=5.0, max_zip_members=100)
    from coscientist.core.execution.input_limits import check_user_inputs_limits

    bad = check_user_inputs_limits(
        [UserInput(name="z.bz2", content=data, media_type="application/x-bzip2")],
        lim,
    )
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_TOO_LARGE


def test_unsafe_path_nfkc_and_drive():
    from coscientist.core.execution.archive_limits import _is_unsafe_path

    assert _is_unsafe_path("../x")
    assert _is_unsafe_path("C:/Windows/x")
    assert _is_unsafe_path("a\x00b")
    assert _is_unsafe_path("/abs")
    # Fullwidth dots that NFKC-normalize toward ``..``
    assert _is_unsafe_path("\uff0e\uff0e/x")
    # Unicode slash / zero-width that must not bypass ``..`` checks
    assert _is_unsafe_path("..\u2215x")
    assert _is_unsafe_path("x/\u200b../y")


@pytest.mark.asyncio
async def test_api_corrupt_deflate_study_finalized_not_500(tmp_path, catalog):
    """Corrupt deflate in POST body → StudyFinalized(refused), never HTTP 500."""
    import base64

    from coscientist.api.studies import post_studies

    data = _corrupt_deflate_zip()
    body = {
        "request": "analyse corrupt zip",
        "inputs": [
            {
                "name": "c.zip",
                "content": base64.b64encode(data).decode(),
                "encoding": "base64",
                "media_type": "application/zip",
            }
        ],
    }
    out = await post_studies(
        tmp_path,
        body,
        catalog=catalog,
        profile_input_limits=InputLimits(max_bytes=50_000_000, max_wire_bytes=5_000_000),
    )
    sid = out["study_id"]
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    events = await rt.store.read_all()
    _assert_one_study_refused(events, kind=RefusalKind.INPUT_INVALID.value)
    _assert_one_study_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_prior_attempt_missing_digest_version_drift_unknown(tmp_path, clock, runtime_cls):
    """Prior attempt missing digest_version → input_drift_unknown (writer seed; #116)."""
    from coscientist.core.claims.reasons import CloseReason
    from coscientist.core.events.events import EventEnvelope, make_idem

    sid = "st_v7_replay"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            )
        ],
        questions=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    fake_aid = f"{task.task_id}/a1"
    # Write a raw v7 journal row (pre-PR19) then close it.
    await rt.writer.append_many([
        EventEnvelope(
            study_id=sid,
            type="AttemptStarted",
            entity_key=fake_aid,
            idem_key=make_idem("AttemptStarted", fake_aid),
            payload={
                "attempt_id": fake_aid,
                "n": 1,
                "dispatch_id": f"dispatch:{fake_aid}",
                "route_returned": False,
                "task_id": task.task_id,
                "study_id": sid,
                "provenance": "real",
                "closed": False,
                "inputs": [
                    {"input": "x", "artifact_id": "art_old", "source": "real"}
                ],
                "tainted_context": False,
                "limitations": [],
                "attempt_role": "agent",
            },
            ts=clock.now(),
            schema_version=writer_schema_version("AttemptStarted"),
        )
    ])
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptClosed",
            entity_key=fake_aid,
            idem_key=make_idem("AttemptClosed", fake_aid),
            payload={
                "attempt_id": fake_aid,
                "task_id": task.task_id,
                "close_reason": CloseReason.FAILED.value,
                "closed": True,
            },
            ts=clock.now(),
            schema_version=2,
        )
    )
    await close_runtime(rt)

    # Reopen → store upcasts v7→v8 (digest_version=None).
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    prior = rt2.attempts.get(fake_aid) or {}
    assert prior.get("inputs")
    assert prior["inputs"][0].get("digest_version") is None
    aid2 = await rt2.start_attempt("E1")
    started2 = next(
        e
        for e in await rt2.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid2
    )
    assert started2.payload["provenance"] == "real"
    lims = started2.payload.get("limitations") or []
    texts = [x.get("text") if isinstance(x, dict) else None for x in lims]
    assert "input_drift_unknown" in texts
    assert "input_drift" not in texts
    await close_runtime(rt2)


def test_study_created_writer_is_v11_after_loopspec():
    """#24 owns max_loop_iter (v10); #23 writer was v11; PR20 stacks v12."""
    from coscientist.core.events.events import STUDY_CREATED_SCHEMA_VERSION
    from coscientist.core.execution.domain import Study

    assert "max_loop_iter" in Study.model_fields
    assert "max_proposals" in Study.model_fields
    assert STUDY_CREATED_SCHEMA_VERSION == writer_schema_version("StudyCreated")


def test_tar_many_members_early_exit():
    """DoS: iterate members lazily — exit at max_zip_members without getmembers()."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    buf = io.BytesIO()
    n = 20_000
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(n):
            info = tarfile.TarInfo(name=f"e{i}")
            info.size = 0
            tf.addfile(info)
    data = buf.getvalue()
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name="many.tar",
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert "members" in bad[1]
    assert elapsed < 3.0


def test_tar_declared_huge_size_early_exit():
    """DoS: member declared 3GB must fail on declared size before streaming content."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    info = tarfile.TarInfo(name="big")
    info.size = 3_000_000_000
    data = info.tobuf() + (b"\x00" * 1024)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=10_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name="huge.tar",
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert elapsed < 1.0


def test_valid_tar_bz2_and_tar_xz_accepted():
    import bz2
    import lzma

    from coscientist.core.execution.archive_limits import detect_archive_kind, inspect_archive

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="a.txt")
        payload = b"hello tar member"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    raw = tar_buf.getvalue()

    bz = bz2.compress(raw)
    assert detect_archive_kind(bz, "application/x-bzip2", name="x.tar.bz2") == "tar.bz2"
    assert detect_archive_kind(bz, "", name="x.tbz2") == "tar.bz2"
    assert (
        inspect_archive(
            bz,
            max_bytes=1_000_000,
            max_unpack_ratio=100.0,
            max_zip_members=100,
            name="x.tar.bz2",
            media_type="application/x-bzip2",
        )
        is None
    )

    xz = lzma.compress(raw)
    assert detect_archive_kind(xz, "application/x-xz", name="x.tar.xz") == "tar.xz"
    assert (
        inspect_archive(
            xz,
            max_bytes=1_000_000,
            max_unpack_ratio=100.0,
            max_zip_members=100,
            name="x.tar.xz",
            media_type="application/x-xz",
        )
        is None
    )


def test_stale_digest_version_garbage_fails_closed():
    from coscientist.core.execution.input_limits import bindings_have_stale_digest_version

    assert bindings_have_stale_digest_version(
        [{"input": "x", "digest": "abc", "digest_version": "not-an-int"}]
    )
    assert bindings_have_stale_digest_version(
        [{"input": "x", "digest": "abc", "digest_version": None}]
    )


@pytest.mark.asyncio
async def test_prior_attempt_legacy_digest_drift_unknown(
    tmp_path, clock, runtime_cls
):
    """Prior attempt with digest, no digest_version → input_drift_unknown (#116).

    Absent digest_version → input_drift_unknown without partial (not empty digest).
    """
    from coscientist.core.claims.reasons import CloseReason
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.events.schema_versions import STUDY_CREATED_SCHEMA_VERSION

    sid = "st_v9_digest"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    # Rewrite StudyCreated as schema_version=9 with non-default max_loop_iter.
    events = await rt.store.read_all()
    created = next(e for e in events if e.type == "StudyCreated")
    v9_payload = dict(created.payload)
    v9_payload.pop("as_of", None)
    v9_payload.pop("user_input_ids", None)
    v9_payload["max_loop_iter"] = 2
    # Upcast continuity: v9 → v10 keeps 2; v11 adds as_of/ids and keeps 2.
    v_after, up = upcast("StudyCreated", 9, dict(v9_payload))
    assert v_after == STUDY_CREATED_SCHEMA_VERSION
    assert up.get("max_loop_iter") == 2
    assert "user_input_ids" in up
    assert "as_of" in up

    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            )
        ],
        questions=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    fake_aid = f"{task.task_id}/a1"
    old_digest = "a" * 64  # non-empty legacy formula digest, no digest_version
    await rt.writer.append_many([
        EventEnvelope(
            study_id=sid,
            type="AttemptStarted",
            entity_key=fake_aid,
            idem_key=make_idem("AttemptStarted", fake_aid),
            payload={
                "attempt_id": fake_aid,
                "n": 1,
                "dispatch_id": f"dispatch:{fake_aid}",
                "route_returned": False,
                "task_id": task.task_id,
                "study_id": sid,
                "provenance": "real",
                "closed": False,
                "inputs": [
                    {
                        "input": "x",
                        "artifact_id": "art_old",
                        "source": "real",
                        "digest": old_digest,
                    }
                ],
                "tainted_context": False,
                "limitations": [],
                "attempt_role": "agent",
            },
            ts=clock.now(),
            schema_version=writer_schema_version("AttemptStarted"),
        )
    ])
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptClosed",
            entity_key=fake_aid,
            idem_key=make_idem("AttemptClosed", fake_aid),
            payload={
                "attempt_id": fake_aid,
                "task_id": task.task_id,
                "close_reason": CloseReason.FAILED.value,
                "closed": True,
            },
            ts=clock.now(),
            schema_version=2,
        )
    )
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    prior = rt2.attempts.get(fake_aid) or {}
    assert prior.get("inputs")
    assert prior["inputs"][0].get("digest") == old_digest
    assert prior["inputs"][0].get("digest_version") is None
    aid2 = await rt2.start_attempt("E1")
    started2 = next(
        e
        for e in await rt2.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid2
    )
    assert started2.payload["provenance"] == "real"
    lims = started2.payload.get("limitations") or []
    texts = [x.get("text") if isinstance(x, dict) else None for x in lims]
    assert "input_drift_unknown" in texts
    assert "input_drift" not in texts
    await close_runtime(rt2)


def _zip_encrypted() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", b"hello world data")
    data = bytearray(buf.getvalue())
    idx = data.find(b"PK\x03\x04")
    data[idx + 6] |= 0x01
    cidx = data.find(b"PK\x01\x02")
    data[cidx + 8] |= 0x01
    return bytes(data)


def _zip_method_99() -> bytes:
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", b"hello world data")
    data = bytearray(buf.getvalue())
    idx = data.find(b"PK\x03\x04")
    struct.pack_into("<H", data, idx + 8, 99)
    cidx = data.find(b"PK\x01\x02")
    struct.pack_into("<H", data, cidx + 10, 99)
    return bytes(data)


def _zip_corrupt_bzip2_member() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_BZIP2) as zf:
        zf.writestr("a.txt", b"hello world" * 50)
    data = bytearray(buf.getvalue())
    data[len(data) // 2] ^= 0xFF
    return bytes(data)


@pytest.mark.parametrize(
    "factory,token",
    [
        (_zip_encrypted, "RuntimeError"),
        (_zip_method_99, "NotImplementedError"),
        (_zip_corrupt_bzip2_member, "OSError"),
    ],
)
def test_zip_member_errors_input_invalid(factory, token):
    from coscientist.core.execution.archive_limits import inspect_zip_streaming

    bad = inspect_zip_streaming(
        factory(),
        max_bytes=50_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name="bad.zip",
    )
    assert bad is not None
    assert bad[0] == RefusalKind.INPUT_INVALID
    assert token in bad[1]


@pytest.mark.asyncio
async def test_api_zip_member_errors_study_finalized_not_500(tmp_path, catalog):
    """Encrypted / method-99 / corrupt-bzip2 zip → StudyFinalized, never 500."""
    import base64

    from coscientist.api.studies import post_studies

    for factory in (_zip_encrypted, _zip_method_99, _zip_corrupt_bzip2_member):
        data = factory()
        body = {
            "request": "analyse bad zip",
            "inputs": [
                {
                    "name": "bad.zip",
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64",
                    "media_type": "application/zip",
                }
            ],
        }
        out = await post_studies(
            tmp_path,
            body,
            catalog=catalog,
            profile_input_limits=InputLimits(
                max_bytes=50_000_000, max_wire_bytes=5_000_000
            ),
        )
        sid = out["study_id"]
        rt = await open_runtime(tmp_path, sid, catalog=catalog)
        events = await rt.store.read_all()
        _assert_one_study_refused(events, kind=RefusalKind.INPUT_INVALID.value)
        _assert_one_study_finalized(events)
        await close_runtime(rt)


def test_tar_longname_capped_no_oom():
    """GNU longname declaring 200MB must reject before reading payload."""
    import time

    from coscientist.core.execution.archive_limits import (
        MAX_TAR_XATTR_BYTES,
        inspect_tar_streaming,
    )

    info = tarfile.TarInfo(name="././@LongLink")
    info.type = tarfile.GNUTYPE_LONGNAME
    info.size = 200 * 1024 * 1024
    data = info.tobuf() + (b"\x00" * 1024)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name="long.tar",
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert str(MAX_TAR_XATTR_BYTES) in bad[1]
    assert elapsed < 0.5


@pytest.mark.parametrize("mode,wrap", [
    ("r:gz", lambda raw: gzip.compress(raw)),
    ("r:bz2", lambda raw: __import__("bz2").compress(raw)),
    ("r:xz", lambda raw: __import__("lzma").compress(raw)),
])
def test_tar_many_members_early_exit_compressed(mode, wrap):
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(5_000):
            info = tarfile.TarInfo(name=f"e{i}")
            info.size = 0
            tf.addfile(info)
    data = wrap(buf.getvalue())
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=50,
        name=f"many.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert "members" in bad[1]
    assert elapsed < 5.0


@pytest.mark.parametrize("mode,wrap", [
    ("r:gz", lambda raw: gzip.compress(raw)),
    ("r:bz2", lambda raw: __import__("bz2").compress(raw)),
    ("r:xz", lambda raw: __import__("lzma").compress(raw)),
])
def test_tar_declared_huge_size_compressed(mode, wrap):
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    info = tarfile.TarInfo(name="big")
    info.size = 3_000_000_000
    raw = info.tobuf() + (b"\x00" * 1024)
    data = wrap(raw)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=10_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"huge.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert elapsed < 2.0


def test_tar_bz2_xz_detected_without_extension():
    import bz2
    import lzma

    from coscientist.core.execution.archive_limits import detect_archive_kind, inspect_archive

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="a.txt")
        payload = b"hello tar member"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    raw = tar_buf.getvalue()
    bz = bz2.compress(raw)
    xz = lzma.compress(raw)
    assert detect_archive_kind(bz, "application/octet-stream", name="blob") == "tar.bz2"
    assert detect_archive_kind(xz, "application/octet-stream", name="blob") == "tar.xz"
    assert (
        inspect_archive(
            bz,
            max_bytes=1_000_000,
            max_unpack_ratio=100.0,
            max_zip_members=100,
            name="blob",
            media_type="application/octet-stream",
        )
        is None
    )
    assert (
        inspect_archive(
            xz,
            max_bytes=1_000_000,
            max_unpack_ratio=100.0,
            max_zip_members=100,
            name="blob",
            media_type="application/octet-stream",
        )
        is None
    )


def test_truncated_gzip_xz_report_corrupt_not_ratio():
    import lzma

    from coscientist.core.execution.archive_limits import (
        inspect_gzip_streaming,
        inspect_xz_streaming,
    )

    # Highly compressible + tight ratio: truncation must still be corrupt, not ratio.
    payload = b"x" * 500_000
    gz = gzip.compress(payload)[:40]
    bad = inspect_gzip_streaming(gz, max_bytes=50_000, max_unpack_ratio=1.5)
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "corrupt" in bad[1]
    assert "ratio" not in bad[1]

    xz = lzma.compress(payload)[:60]
    bad2 = inspect_xz_streaming(xz, max_bytes=50_000, max_unpack_ratio=1.5)
    assert bad2 is not None and bad2[0] == RefusalKind.INPUT_INVALID
    assert "corrupt" in bad2[1]
    assert "ratio" not in bad2[1]


def _ustar_header(
    name: str, size: int, typeflag: str = "0", linkname: str = ""
) -> bytes:
    h = bytearray(512)
    nb = name.encode()[:100]
    h[0 : len(nb)] = nb
    h[124:136] = f"{size:011o}\x00".encode()
    h[156] = ord(typeflag)
    lb = linkname.encode()[:100]
    h[157 : 157 + len(lb)] = lb
    h[257:263] = b"ustar\x00"
    h[263:265] = b"00"
    h[148:156] = b"        "
    chk = sum(h)
    h[148:156] = f"{chk:06o}\x00 ".encode()
    return bytes(h)


def _pax_block(records: dict[str, str], typeflag: str = "x") -> bytes:
    payload = b""
    for k, v in records.items():
        kv = f"{k}={v}\n".encode()
        n = len(kv) + 3
        for _ in range(6):
            prefix = f"{n} ".encode()
            if len(prefix) + len(kv) == n:
                payload += prefix + kv
                break
            n = len(prefix) + len(kv)
        else:
            raise RuntimeError(k)
    size = len(payload)
    pad = (512 - (size % 512)) % 512
    return (
        _ustar_header("././@PaxHeader", size, typeflag=typeflag)
        + payload
        + (b"\x00" * pad)
    )


def test_pax_path_traversal_tgz_rejected():
    """ustar safe.txt + pax path=../../etc/evil must not be accepted."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    body = b"hello safe"
    info = tarfile.TarInfo(name="safe.txt")
    info.size = len(body)
    info.pax_headers = {"path": "../../etc/evil"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        tf.addfile(info, io.BytesIO(body))
    data = gzip.compress(buf.getvalue())
    bad = inspect_tar_streaming(
        data,
        max_bytes=1_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name="upload.tgz",
        mode="r:gz",
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "unsafe tar path" in bad[1]
    assert "../../etc/evil" in bad[1]


def test_pax_abs_path_txz_rejected():
    import lzma

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    body = b"hello"
    info = tarfile.TarInfo(name="safe.txt")
    info.size = len(body)
    info.pax_headers = {"path": "/abs/evil"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        tf.addfile(info, io.BytesIO(body))
    data = lzma.compress(buf.getvalue())
    bad = inspect_tar_streaming(
        data,
        max_bytes=1_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name="evil.txz",
        mode="r:xz",
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID


def test_pax_size_zero_hides_symlink_rejected():
    """pax size=0 over ustar 512 (symlink body) — pax size key rejected."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    sym = _ustar_header("../../evil_link", 0, typeflag="2", linkname="/etc/passwd")
    body = sym + (b"\x00" * (512 - len(sym)))
    archive = (
        _pax_block({"size": "0"})
        + _ustar_header("safe.txt", 512, typeflag="0")
        + body
        + (b"\x00" * 1024)
    )
    bad = inspect_tar_streaming(
        archive,
        max_bytes=1_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name="hide_sym.tar",
        mode="r:*",
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "pax size" in bad[1]


def test_pax_size_override_hides_nested_zip_rejected():
    """pax size over ustar size 0 (nested zip body) — pax size key rejected."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("inner.txt", b"nested")
    zpad = zbuf.getvalue()
    zpad = zpad + (b"\x00" * (512 - (len(zpad) % 512)))
    zpad = zpad[:512]
    archive = (
        _pax_block({"size": "512"})
        + _ustar_header("empty.txt", 0, typeflag="0")
        + zpad
        + (b"\x00" * 1024)
    )
    bad = inspect_tar_streaming(
        archive,
        max_bytes=1_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name="hide_zip.tar",
        mode="r:*",
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "pax size" in bad[1]


def _pax_size_fake_eof_huge_longname(*, long_size: int, global_pax: bool = False) -> bytes:
    """Attack: pax size=1024 + ustar size 0 + 1024 zero bytes + huge GNU L.

    Layer (a) that skips by ustar size would false-EOF on the zeros; tarfile
    would then read the huge longname. We reject pax ``size`` before that.
    """
    pax = _pax_block({"size": "1024"}, typeflag="g" if global_pax else "x")
    member = _ustar_header("safe.txt", 0, typeflag="0")
    body = b"\x00" * 1024
    huge_hdr = _ustar_header("././@LongLink", long_size, typeflag="L")
    return pax + member + body + huge_hdr + (b"\x00" * 1024)


@pytest.mark.parametrize(
    "mode,wrap,global_pax,long_size",
    [
        ("r:gz", lambda raw: gzip.compress(raw), False, 200 * 1024 * 1024),
        ("r:bz2", lambda raw: __import__("bz2").compress(raw), False, 200 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), False, 200 * 1024 * 1024),
        ("r:gz", lambda raw: gzip.compress(raw), False, 1024 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), False, 1024 * 1024 * 1024),
        ("r:gz", lambda raw: gzip.compress(raw), True, 200 * 1024 * 1024),
    ],
)
def test_pax_size_oom_bypass_rejected(mode, wrap, global_pax, long_size):
    """gz/bz2/xz: pax size fake-EOF + huge longname must not OOM or accept."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _pax_size_fake_eof_huge_longname(long_size=long_size, global_pax=global_pax)
    data = wrap(raw)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"pax_oom.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "pax size" in bad[1]
    assert elapsed < 2.0


def _gnu_sparse_old_header(name: str, size: int, *, n_ext: int, origsize: int) -> bytes:
    """GNU sparse typeflag S with ``isextended`` and ``origsize`` (old format)."""
    h = bytearray(_ustar_header(name, size, typeflag="S"))
    # frombuf: isextended @482, origsize octal @483:495
    h[482] = 1 if n_ext > 0 else 0
    h[483:495] = f"{origsize:011o}\x00".encode()
    h[148:156] = b"        "
    chk = sum(h)
    h[148:156] = f"{chk:06o}\x00 ".encode()
    return bytes(h)


def _gnu_sparse_ext_block(*, isextended: bool) -> bytes:
    b = bytearray(512)
    b[504] = 1 if isextended else 0
    return bytes(b)


def _gnu_sparse_s_fake_eof_huge_longname(*, long_size: int, n_ext: int = 2) -> bytes:
    """Attack: typeflag S + k ext-blocks sized as ustar body → gate false-EOF.

    Gate that skips only ustar ``size`` treats the ext-blocks as the body, then
    sees 1024 zero bytes as double-zero EOF; ``tarfile._proc_sparse`` reads the
    ext-blocks first and continues into a huge GNU longname.
    """
    size = n_ext * 512
    hdr = _gnu_sparse_old_header("sparse.bin", size, n_ext=n_ext, origsize=size)
    exts = b"".join(
        _gnu_sparse_ext_block(isextended=(i < n_ext - 1)) for i in range(n_ext)
    )
    body_zeros = b"\x00" * 1024
    huge_hdr = _ustar_header("././@LongLink", long_size, typeflag="L")
    return hdr + exts + body_zeros + huge_hdr + (b"\x00" * 1024)


@pytest.mark.parametrize(
    "mode,wrap,long_size",
    [
        ("r:gz", lambda raw: gzip.compress(raw), 200 * 1024 * 1024),
        ("r:bz2", lambda raw: __import__("bz2").compress(raw), 200 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), 200 * 1024 * 1024),
        ("r:gz", lambda raw: gzip.compress(raw), 1024 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), 1024 * 1024 * 1024),
        ("r:bz2", lambda raw: __import__("bz2").compress(raw), 1024 * 1024 * 1024),
    ],
)
def test_gnu_sparse_s_oom_bypass_rejected(mode, wrap, long_size):
    """gz/bz2/xz: GNU sparse S + ext-blocks fake-EOF must not OOM or accept."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _gnu_sparse_s_fake_eof_huge_longname(long_size=long_size)
    data = wrap(raw)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"sparse_s.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "gnu sparse" in bad[1]
    assert elapsed < 2.0


@pytest.mark.parametrize(
    "mode,wrap,records",
    [
        (
            "r:gz",
            lambda raw: gzip.compress(raw),
            {"GNU.sparse.map": "0,0"},
        ),
        (
            "r:bz2",
            lambda raw: __import__("bz2").compress(raw),
            {"GNU.sparse.size": "0"},
        ),
        (
            "r:xz",
            lambda raw: __import__("lzma").compress(raw),
            {
                "GNU.sparse.major": "1",
                "GNU.sparse.minor": "0",
                "GNU.sparse.name": "f",
                "GNU.sparse.realsize": "0",
            },
        ),
    ],
)
def test_pax_gnu_sparse_keys_rejected(mode, wrap, records):
    """gz/bz2/xz: any pax GNU.sparse.* (map / size / 1.0) must be rejected."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    archive = (
        _pax_block(records)
        + _ustar_header("f.txt", 0, typeflag="0")
        + (b"\x00" * 1024)
    )
    bad = inspect_tar_streaming(
        wrap(archive),
        max_bytes=1_000_000,
        max_unpack_ratio=100.0,
        max_zip_members=100,
        name=f"pax_sparse.tar{mode}",
        mode=mode,
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "GNU.sparse" in bad[1]


def _pax_rec(k: str, v: str) -> bytes:
    kv = f"{k}={v}\n".encode()
    n = len(kv) + 3
    for _ in range(8):
        prefix = f"{n} ".encode()
        if len(prefix) + len(kv) == n:
            return prefix + kv
        n = len(prefix) + len(kv)
    raise RuntimeError(k)


def _pax_with_smuggled_in_padding(
    smuggle: dict[str, str],
    *,
    typeflag: str = "x",
    long_size: int | None = None,
) -> bytes:
    """ustar size covers only a benign record; forbidden keys live in pad.

    Gate that parses only ``size`` bytes misses them; ``tarfile._proc_pax``
    reads ``_block(size)`` and parses until NUL.
    """
    benign = _pax_rec("path", "safe.txt")
    smuggled = b"".join(_pax_rec(k, v) for k, v in smuggle.items())
    declared = len(benign)
    block = bytearray(512)
    block[:declared] = benign
    block[declared : declared + len(smuggled)] = smuggled
    hdr = _ustar_header("././@PaxHeader", declared, typeflag=typeflag)
    member = _ustar_header("safe.txt", 0, typeflag="0")
    body = b"\x00" * 1024
    out = bytes(hdr) + bytes(block) + member + body
    if long_size is not None:
        out += _ustar_header("././@LongLink", long_size, typeflag="L") + (b"\x00" * 1024)
    else:
        out += b"\x00" * 1024
    return out


@pytest.mark.parametrize(
    "mode,wrap,typeflag,smuggle,long_size,needle",
    [
        (
            "r:gz",
            lambda raw: gzip.compress(raw),
            "x",
            {"size": "1024"},
            1024 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:bz2",
            lambda raw: __import__("bz2").compress(raw),
            "x",
            {"size": "1024"},
            200 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:xz",
            lambda raw: __import__("lzma").compress(raw),
            "x",
            {"size": "1024"},
            1024 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:gz",
            lambda raw: gzip.compress(raw),
            "g",
            {"size": "1024"},
            1024 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:bz2",
            lambda raw: __import__("bz2").compress(raw),
            "g",
            {"size": "1024"},
            200 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:xz",
            lambda raw: __import__("lzma").compress(raw),
            "g",
            {"size": "1024"},
            1024 * 1024 * 1024,
            "pax size",
        ),
        (
            "r:gz",
            lambda raw: gzip.compress(raw),
            "x",
            {
                "GNU.sparse.major": "1",
                "GNU.sparse.minor": "0",
                "GNU.sparse.name": "f",
                "GNU.sparse.realsize": "0",
            },
            None,
            "GNU.sparse",
        ),
        (
            "r:bz2",
            lambda raw: __import__("bz2").compress(raw),
            "g",
            {
                "GNU.sparse.major": "1",
                "GNU.sparse.minor": "0",
                "GNU.sparse.name": "f",
                "GNU.sparse.realsize": "0",
            },
            None,
            "GNU.sparse",
        ),
        (
            "r:xz",
            lambda raw: __import__("lzma").compress(raw),
            "x",
            {
                "GNU.sparse.major": "1",
                "GNU.sparse.minor": "0",
                "GNU.sparse.name": "f",
                "GNU.sparse.realsize": "0",
            },
            None,
            "GNU.sparse",
        ),
        (
            "r:gz",
            lambda raw: gzip.compress(raw),
            "g",
            {"GNU.sparse.map": "0,0"},
            None,
            "GNU.sparse",
        ),
        (
            "r:xz",
            lambda raw: __import__("lzma").compress(raw),
            "x",
            {"GNU.sparse.size": "0"},
            None,
            "GNU.sparse",
        ),
        (
            "r:bz2",
            lambda raw: __import__("bz2").compress(raw),
            "g",
            {"GNU.sparse.size": "0"},
            200 * 1024 * 1024,
            "GNU.sparse",
        ),
    ],
)
def test_pax_keys_smuggled_in_padding_rejected(
    mode, wrap, typeflag, smuggle, long_size, needle
):
    """gz/bz2/xz × x/g: keys in pax padding must not bypass the resource gate."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _pax_with_smuggled_in_padding(
        smuggle, typeflag=typeflag, long_size=long_size
    )
    data = wrap(raw)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"pax_pad_{typeflag}.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert needle in bad[1]
    assert elapsed < 2.0


def _dir_body_smuggles_longname(*, v7_dir: bool, long_size: int) -> bytes:
    """z11: empty file + DIR (or V7 AREGTYPE name/) with size=512 body = huge L."""
    cover = _ustar_header("cover.txt", 0, typeflag="0")
    if v7_dir:
        diri = bytearray(_ustar_header("d/", 512, typeflag="0"))
        diri[156] = 0  # AREGTYPE — tarfile treats name/ as directory
        diri[148:156] = b"        "
        diri[148:156] = f"{sum(diri):06o}\x00 ".encode()
        diri_b = bytes(diri)
    else:
        diri_b = _ustar_header("d", 512, typeflag="5")
    huge = _ustar_header("././@LongLink", long_size, typeflag="L")
    return cover + diri_b + huge + (b"\x00" * 1024)


@pytest.mark.parametrize(
    "mode,wrap,v7_dir,long_size",
    [
        ("r:gz", lambda raw: gzip.compress(raw), False, 1024 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), False, 1024 * 1024 * 1024),
        ("r:bz2", lambda raw: __import__("bz2").compress(raw), False, 200 * 1024 * 1024),
        ("r:gz", lambda raw: gzip.compress(raw), True, 1024 * 1024 * 1024),
        ("r:xz", lambda raw: __import__("lzma").compress(raw), True, 1024 * 1024 * 1024),
        ("r:bz2", lambda raw: __import__("bz2").compress(raw), True, 200 * 1024 * 1024),
    ],
)
def test_dir_body_smuggled_longname_rejected(mode, wrap, v7_dir, long_size):
    """z11: DIR/V7-dir size>0 body must not skip smuggled huge GNU L (tarfile-driven)."""
    import time

    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _dir_body_smuggles_longname(v7_dir=v7_dir, long_size=long_size)
    data = wrap(raw)
    t0 = time.perf_counter()
    bad = inspect_tar_streaming(
        data,
        max_bytes=50_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"z11_v7{v7_dir}.tar{mode}",
        mode=mode,
    )
    elapsed = time.perf_counter() - t0
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "tar longname/pax" in bad[1]
    assert elapsed < 2.0


def _legit_tar_bytes(kind: str) -> tuple[bytes, str]:
    """Build one of 12 legitimate small archives → (blob, inspect mode)."""
    import bz2
    import lzma

    def _one_file(name: str = "ok.txt", body: bytes = b"hello") -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        return buf.getvalue()

    def _dir_and_file() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            d = tarfile.TarInfo("sub")
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            tf.addfile(d)
            info = tarfile.TarInfo("sub/a.txt")
            info.size = 3
            tf.addfile(info, io.BytesIO(b"abc"))
        return buf.getvalue()

    def _multi() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for n, b in (("a.txt", b"1"), ("b.txt", b"22"), ("c.txt", b"333")):
                info = tarfile.TarInfo(n)
                info.size = len(b)
                tf.addfile(info, io.BytesIO(b))
        return buf.getvalue()

    table = {
        "tar_file": (_one_file(), "r:"),
        "tar_dir_file": (_dir_and_file(), "r:"),
        "tar_empty_file": (_one_file("e", b""), "r:"),
        "tar_utf8": (_one_file("café.txt", "ok".encode()), "r:"),
        "tgz_file": (gzip.compress(_one_file()), "r:gz"),
        "tgz_dir": (gzip.compress(_dir_and_file()), "r:gz"),
        "tgz_multi": (gzip.compress(_multi()), "r:gz"),
        "tbz_file": (bz2.compress(_one_file()), "r:bz2"),
        "tbz_dir": (bz2.compress(_dir_and_file()), "r:bz2"),
        "txz_file": (lzma.compress(_one_file()), "r:xz"),
        "txz_dir": (lzma.compress(_dir_and_file()), "r:xz"),
        "tar_pax_path": (
            _pax_block({"path": "safe.txt"})
            + _ustar_header("safe.txt", 4, typeflag="0")
            + b"data"
            + (b"\x00" * 508)
            + (b"\x00" * 1024),
            "r:",
        ),
    }
    return table[kind]


@pytest.mark.parametrize(
    "kind",
    [
        "tar_file",
        "tar_dir_file",
        "tar_empty_file",
        "tar_utf8",
        "tgz_file",
        "tgz_dir",
        "tgz_multi",
        "tbz_file",
        "tbz_dir",
        "txz_file",
        "txz_dir",
        "tar_pax_path",
    ],
)
def test_legitimate_tar_archives_accepted(kind):
    """12 legitimate tar variants must pass the tarfile-driven gate."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    data, mode = _legit_tar_bytes(kind)
    bad = inspect_tar_streaming(
        data,
        max_bytes=1_000_000,
        max_unpack_ratio=1000.0,
        max_zip_members=100,
        name=f"legit_{kind}",
        mode=mode,  # type: ignore[arg-type]
    )
    assert bad is None, bad


def _nested_meta_chain(n: int, typeflag: str = "x") -> bytes:
    """``n`` consecutive empty meta headers + empty file + double-zero EOF."""
    name = "././@LongLink" if typeflag in ("L", "K") else "././@PaxHeader"
    return (
        b"".join(_ustar_header(name, 0, typeflag=typeflag) for _ in range(n))
        + _ustar_header("e.txt", 0, typeflag="0")
        + (b"\x00" * 1024)
    )


def _meta_wrap_specs() -> list[tuple[str, object, str, str]]:
    """(inspect_mode, wrap, filename, media_type) for plain/gz/xz/bz2."""
    import bz2
    import lzma

    return [
        ("r:", lambda raw: raw, "meta.tar", "application/x-tar"),
        ("r:gz", gzip.compress, "meta.tgz", "application/gzip"),
        ("r:xz", lzma.compress, "meta.txz", "application/x-xz"),
        ("r:bz2", bz2.compress, "meta.tbz2", "application/x-bzip2"),
    ]


@pytest.mark.parametrize("typeflag", ["x", "L", "K", "g"])
@pytest.mark.parametrize("mode,wrap,fname,mt", _meta_wrap_specs())
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_nested_meta_chain_over_limit_rejected(via, mode, wrap, fname, mt, typeflag):
    """B5: both gate paths × x/L/K/g × gz/xz/bz2/plain → INPUT_INVALID, no raise."""
    from coscientist.core.execution.archive_limits import MAX_TAR_META_CHAIN, inspect_tar_streaming

    raw = _nested_meta_chain(MAX_TAR_META_CHAIN + 1, typeflag=typeflag)
    data = wrap(raw)
    lim = InputLimits(max_bytes=10_000_000, max_unpack_ratio=100.0, max_zip_members=10_000)
    if via == "inspect_tar_streaming":
        bad = inspect_tar_streaming(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name=fname,
            mode=mode,  # type: ignore[arg-type]
        )
    else:
        bad = check_user_inputs_limits(
            [UserInput(name=fname, content=data, media_type=mt)],
            lim,
        )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "meta chain" in bad[1]


@pytest.mark.parametrize("typeflag", ["x", "L", "K", "g"])
@pytest.mark.parametrize("mode,wrap,fname,mt", _meta_wrap_specs())
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_deep_nested_meta_2000_no_recursion_error(via, mode, wrap, fname, mt, typeflag):
    """B5 repro: 2000× nested meta — both paths refuse, never RecursionError."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _nested_meta_chain(2000, typeflag=typeflag)
    data = wrap(raw)
    lim = InputLimits(max_bytes=50_000_000, max_unpack_ratio=100.0, max_zip_members=10_000)
    if via == "inspect_tar_streaming":
        bad = inspect_tar_streaming(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name=fname,
            mode=mode,  # type: ignore[arg-type]
        )
    else:
        bad = check_user_inputs_limits(
            [UserInput(name=fname, content=data, media_type=mt)],
            lim,
        )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "meta chain" in bad[1]


@pytest.mark.parametrize(
    "mode,wrap,fname,mt",
    [
        ("r:gz", gzip.compress, "meta_ok.tgz", "application/gzip"),
        ("r:", lambda raw: raw, "meta_ok.tar", "application/x-tar"),
    ],
)
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_nested_meta_within_limit_accepted(via, mode, wrap, fname, mt):
    """Exactly MAX_TAR_META_CHAIN empty pax headers still accept on both paths."""
    from coscientist.core.execution.archive_limits import MAX_TAR_META_CHAIN, inspect_tar_streaming

    raw = _nested_meta_chain(MAX_TAR_META_CHAIN, typeflag="x")
    data = wrap(raw)
    lim = InputLimits(max_bytes=10_000_000, max_unpack_ratio=100.0, max_zip_members=10_000)
    if via == "inspect_tar_streaming":
        bad = inspect_tar_streaming(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name=fname,
            mode=mode,  # type: ignore[arg-type]
        )
    else:
        bad = check_user_inputs_limits(
            [UserInput(name=fname, content=data, media_type=mt)],
            lim,
        )
    assert bad is None, bad


def test_many_small_pax_headers_budget_too_large():
    """Many tiny pax headers hit byte budget → INPUT_TOO_LARGE (not ReadError)."""
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    # Depth limit fires at 9; keep chain short and repeat pax+file pairs so
    # uncompressed header bytes exceed max_bytes via the counting reader.
    unit = _pax_block({"path": "f.txt"}) + _ustar_header("f.txt", 0)
    # ~1KB per unit; 40 units → ~40KB uncompressed
    raw = unit * 40 + (b"\x00" * 1024)
    bad = inspect_tar_streaming(
        raw,
        max_bytes=8_000,
        max_unpack_ratio=100.0,
        max_zip_members=10_000,
        name="many_pax.tar",
        mode="r:",
    )
    assert bad is not None and bad[0] == RefusalKind.INPUT_TOO_LARGE
    assert "uncompressed" in bad[1] and "max_bytes" in bad[1]

    # Via check_user_inputs_limits: use tgz so wire/blob size is under max_bytes
    # and the streaming count (not the outer len(data) gate) fires.
    tgz = gzip.compress(raw)
    assert len(tgz) < 8_000
    bad2 = check_user_inputs_limits(
        [UserInput(name="many_pax.tgz", content=tgz, media_type="application/gzip")],
        InputLimits(max_bytes=8_000, max_unpack_ratio=100.0, max_zip_members=10_000),
    )
    assert bad2 is not None and bad2[0] == RefusalKind.INPUT_TOO_LARGE
    assert "uncompressed" in bad2[1] and "max_bytes" in bad2[1]


@pytest.mark.parametrize("typeflag", ["x", "L", "K", "g"])
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_z13_z14_nested_meta_2000_both_paths(via, typeflag):
    """z13/z14: ~2000 consecutive x/L/K/g → INPUT_INVALID, no RecursionError.

    Both ``inspect_tar_streaming`` and ``check_user_inputs_limits``.
    """
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    raw = _nested_meta_chain(2000, typeflag=typeflag)
    data = gzip.compress(raw)
    lim = InputLimits(max_bytes=50_000_000, max_unpack_ratio=100.0, max_zip_members=10_000)
    if via == "inspect_tar_streaming":
        bad = inspect_tar_streaming(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name="z13.tgz",
            mode="r:gz",
        )
    else:
        bad = check_user_inputs_limits(
            [UserInput(name="z14.tgz", content=data, media_type="application/gzip")],
            lim,
        )
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "meta chain" in bad[1]


def _meta_boundary_lk_g_xz_bz2_specs() -> list[tuple[str, object, str, str]]:
    import bz2
    import lzma

    return [
        ("r:xz", lzma.compress, "b.txz", "application/x-xz"),
        ("r:bz2", bz2.compress, "b.tbz2", "application/x-bzip2"),
    ]


def _check_meta_chain_via(via: str, data: bytes, *, mode: str, fname: str, mt: str):
    from coscientist.core.execution.archive_limits import inspect_tar_streaming

    lim = InputLimits(max_bytes=10_000_000, max_unpack_ratio=100.0, max_zip_members=10_000)
    if via == "inspect_tar_streaming":
        return inspect_tar_streaming(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name=fname,
            mode=mode,  # type: ignore[arg-type]
        )
    return check_user_inputs_limits(
        [UserInput(name=fname, content=data, media_type=mt)],
        lim,
    )


@pytest.mark.parametrize("typeflag", ["L", "K", "g"])
@pytest.mark.parametrize("mode,wrap,fname,mt", _meta_boundary_lk_g_xz_bz2_specs())
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_meta_chain_depth_8_accepted_lk_g_xz_bz2(via, mode, wrap, fname, mt, typeflag):
    """Boundary depth 8: L/K/g on xz/bz2 accepted on both gate paths."""
    from coscientist.core.execution.archive_limits import MAX_TAR_META_CHAIN

    data = wrap(_nested_meta_chain(MAX_TAR_META_CHAIN, typeflag=typeflag))
    bad = _check_meta_chain_via(via, data, mode=mode, fname=fname, mt=mt)
    assert bad is None, bad


@pytest.mark.parametrize("typeflag", ["L", "K", "g"])
@pytest.mark.parametrize("mode,wrap,fname,mt", _meta_boundary_lk_g_xz_bz2_specs())
@pytest.mark.parametrize("via", ["inspect_tar_streaming", "check_user_inputs_limits"])
def test_meta_chain_depth_9_rejected_lk_g_xz_bz2(via, mode, wrap, fname, mt, typeflag):
    """Boundary depth 9: L/K/g on xz/bz2 → INPUT_INVALID on both gate paths."""
    from coscientist.core.execution.archive_limits import MAX_TAR_META_CHAIN

    data = wrap(_nested_meta_chain(MAX_TAR_META_CHAIN + 1, typeflag=typeflag))
    bad = _check_meta_chain_via(via, data, mode=mode, fname=fname, mt=mt)
    assert bad is not None and bad[0] == RefusalKind.INPUT_INVALID
    assert "meta chain" in bad[1]


@pytest.mark.asyncio
async def test_nested_meta_intake_finalized_store_rejects_late_write(
    tmp_path, clock, runtime_cls
):
    """PR19 intake refuse → one StudyFinalized; #31 store guard blocks later writes."""
    from coscientist.core.events.errors import StudyClosedError
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.execution.archive_limits import MAX_TAR_META_CHAIN

    sid = "st_b5_meta_closed"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    raw = _nested_meta_chain(MAX_TAR_META_CHAIN + 1, typeflag="x")
    data = gzip.compress(raw)
    await _create_with_inputs(
        rt,
        profile="research",
        request="analyse nested meta",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        inputs=[
            UserInput(name="evil.tgz", content=data, media_type="application/gzip")
        ],
        input_limits=InputLimits(
            max_bytes=10_000_000, max_unpack_ratio=100.0, max_zip_members=10_000
        ),
    )
    events = await rt.store.read_all()
    types = [e.type for e in events]
    _assert_one_study_refused(events)
    fin = _assert_one_study_finalized(events)
    assert fin.payload["terminal_reason"] == TerminalReason.REFUSED.value
    assert fin.payload["trigger"] == "intake"
    # StudyFinalized is last — PR19 must not append after it.
    assert types[-1] == "StudyFinalized"
    n_before = len(events)
    late = EventEnvelope(
        study_id=sid,
        type="GraphNodeUpserted",
        entity_key=f"{sid}:late",
        idem_key=make_idem("GraphNodeUpserted", f"{sid}:late"),
        payload={"node_id": f"{sid}:late", "kind": "study", "study_id": sid},
        ts=clock.now(), schema_version=writer_schema_version("GraphNodeUpserted"), 
    )
    with pytest.raises(StudyClosedError):
        await rt.store.append(late)
    events2 = await rt.store.read_all()
    assert len(events2) == n_before
    _assert_one_study_finalized(events2)
    await close_runtime(rt)
