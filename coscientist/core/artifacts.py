"""Content-addressed artifact store with sha256 verify on read (DESIGN §3.8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from coscientist.core.digest import sha256_hex
from coscientist.core.domain import Artifact, ToolCallRef
from coscientist.core.errors import ArtifactCorrupt, ArtifactRejected


class LocalArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.cas = self.root / "cas"
        self.meta = self.root / "meta"
        self.tmp = self.root / "tmp"
        self.cas.mkdir(parents=True, exist_ok=True)
        self.meta.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Artifact] = {}

    def put(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        schema_ref: str | None = None,
        attempt_closed: bool = False,
    ) -> Artifact:
        if attempt_closed:
            raise ArtifactRejected("attempt_closed")
        digest = sha256_hex(content)
        blob = self.cas / digest
        if not blob.exists():
            tmp = self.tmp / produced_by.call_id.replace("/", "_").replace(":", "_")
            tmp.write_bytes(content)
            tmp.replace(blob)
        call_part = sha256_hex(produced_by.call_id)[:8]
        artifact_id = f"art_{digest[:16]}_{call_part}"
        art = Artifact(
            artifact_id=artifact_id,
            name=name,
            media_type=media_type,
            sha256=digest,
            size=len(content),
            uri=f"file://{blob}",
            produced_by=produced_by,
            schema_ref=schema_ref,
        )
        self._index[artifact_id] = art
        (self.meta / f"{artifact_id}.json").write_text(
            art.model_dump_json(), encoding="utf-8"
        )
        return art

    def get_meta(self, artifact_id: str) -> Artifact | None:
        if artifact_id in self._index:
            return self._index[artifact_id]
        path = self.meta / f"{artifact_id}.json"
        if not path.exists():
            return None
        art = Artifact.model_validate_json(path.read_text(encoding="utf-8"))
        self._index[artifact_id] = art
        return art

    def read(self, artifact_id: str) -> bytes:
        art = self.get_meta(artifact_id)
        if art is None:
            raise FileNotFoundError(artifact_id)
        blob = self.cas / art.sha256
        data = blob.read_bytes()
        if sha256_hex(data) != art.sha256:
            raise ArtifactCorrupt(f"sha256 mismatch for {artifact_id}")
        return data

    def read_json(self, artifact_id: str) -> object:
        return json.loads(self.read(artifact_id))


def make_tool_ref(call_id: str, tool: str = "local", args: str = "") -> ToolCallRef:
    return ToolCallRef(
        call_id=call_id,
        tool=tool,
        args_digest=sha256_hex(args),
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
