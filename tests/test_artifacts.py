"""Content-addressed artifacts + sha256 verify (DESIGN §3.8)."""

from __future__ import annotations

import pytest

from coscientist.core.events.errors import ArtifactCorrupt, ArtifactRejected
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref


def test_put_read_roundtrip(tmp_path):
    store = LocalArtifactStore(tmp_path)
    art = store.put(
        b"hello",
        make_tool_ref("a#1:1", attempt_id="a#1"),
        name="out",
        media_type="text/plain",
    )
    assert store.read(art.artifact_id) == b"hello"
    assert art.sha256 in art.uri


def test_corrupt_detected(tmp_path):
    store = LocalArtifactStore(tmp_path)
    art = store.put(b"hello", make_tool_ref("a#1:1", attempt_id="a#1"), name="out", media_type="text/plain")
    blob = store.cas / art.sha256
    blob.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorrupt):
        store.read(art.artifact_id)


def test_reject_closed_attempt(tmp_path):
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(ArtifactRejected):
        store.put(
            b"x",
            make_tool_ref("a#1:1", attempt_id="a#1"),
            name="out",
            media_type="text/plain",
            attempt_closed=True,
        )


def test_same_content_different_produced_by(tmp_path):
    store = LocalArtifactStore(tmp_path)
    a1 = store.put(b"same", make_tool_ref("a#1:1", attempt_id="a#1"), name="a", media_type="text/plain")
    a2 = store.put(b"same", make_tool_ref("a#1:2", attempt_id="a#1"), name="b", media_type="text/plain")
    assert a1.sha256 == a2.sha256
    assert a1.artifact_id != a2.artifact_id
    assert len(list(store.cas.iterdir())) == 1
