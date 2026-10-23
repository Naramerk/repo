"""Content-addressed artifact store with sha256 verify on read (DESIGN §3.8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from coscientist.core.digest import sha256_hex
from coscientist.core.domain import (
    Artifact,
    ArtifactProducer,
    ArtifactRole,
    FsIsolation,
    NetIsolation,
    ToolCallRef,
)
from coscientist.core.errors import ArtifactCorrupt, ArtifactRejected
from coscientist.core.taint import derived_tainted


def _infer_artifact_role(media_type: str) -> ArtifactRole:
    """Fallback media_type → role when OutputSpec.role is absent."""
    mt = (media_type or "").lower()
    if any(
        tok in mt
        for tok in (
            "python",
            "javascript",
            "typescript",
            "x-sh",
            "x-shellscript",
            "x-script",
            "x-bytecode",
        )
    ):
        return "code"
    if mt.startswith("text/x-"):
        return "code"
    return "data"


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

    def put_cas(self, content: bytes, *, tmp_name: str = "cas") -> str:
        """Write raw bytes to content-addressed storage; return sha256 hex digest."""
        digest = sha256_hex(content)
        blob = self.cas / digest
        if not blob.exists():
            safe = tmp_name.replace("/", "_").replace(":", "_")
            tmp = self.tmp / f"{safe}.{digest[:12]}"
            tmp.write_bytes(content)
            tmp.replace(blob)
        return digest

    def read_cas(self, digest: str) -> bytes:
        blob = self.cas / digest
        if not blob.exists():
            raise FileNotFoundError(digest)
        data = blob.read_bytes()
        if sha256_hex(data) != digest:
            raise ArtifactCorrupt(f"sha256 mismatch for cas/{digest}")
        return data

    def _store(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        producer: ArtifactProducer,
        schema_ref: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        attempt_closed: bool = False,
        net_isolation: NetIsolation | None = None,
        fs_isolation: FsIsolation | None = None,
        tainted: bool = False,
        context_tainted: bool = False,
    ) -> Artifact:
        if attempt_closed:
            raise ArtifactRejected("attempt_closed")
        digest = self.put_cas(content, tmp_name=produced_by.call_id)
        call_part = sha256_hex(produced_by.call_id)[:8]
        artifact_id = f"art_{digest[:16]}_{call_part}"
        media_role = _infer_artifact_role(media_type)
        # Display role: paper/figure/… honored; code|data only from core/sandbox
        # writers (OutputSpec). Agent ``put`` ignores code|data role args.
        if role in ("paper", "figure", "table", "log", "report"):
            inferred = role
        elif role in ("code", "data") and producer in ("core", "sandbox", "tool"):
            inferred = role
        else:
            inferred = media_role
        # kind is core-stamped only (experiment_design / contradiction_map).
        stamped_kind = kind if kind in ("experiment_design", "contradiction_map") else None
        art = Artifact(
            artifact_id=artifact_id,
            name=name,
            media_type=media_type,
            sha256=digest,
            size=len(content),
            uri=f"file://{self.cas / digest}",
            produced_by=produced_by,
            schema_ref=schema_ref,
            role=inferred,  # type: ignore[arg-type]
            kind=stamped_kind,
            producer=producer,
            net_isolation=net_isolation if producer == "sandbox" else None,
            fs_isolation=fs_isolation if producer == "sandbox" else None,
            # context_tainted arg OR ContextVar (per-attempt; safe under foreach).
            tainted=derived_tainted(context_tainted=context_tainted, explicit=tainted),
        )
        self._index[artifact_id] = art
        (self.meta / f"{artifact_id}.json").write_text(
            art.model_dump_json(), encoding="utf-8"
        )
        return art

    def put(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        schema_ref: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        attempt_closed: bool = False,
        tainted: bool = False,
        context_tainted: bool = False,
    ) -> Artifact:
        """Agent-controlled path — always ``producer=agent`` (never trusted).

        ``kind`` is ignored here (agent cannot stamp experiment_design); use
        ``put_core`` / executor OutputSpec stamping.
        """
        del kind  # agent cannot forge artifact.kind
        return self._store(
            content,
            produced_by,
            name=name,
            media_type=media_type,
            schema_ref=schema_ref,
            role=role,
            kind=None,
            attempt_closed=attempt_closed,
            producer="agent",
            tainted=tainted,
            context_tainted=context_tainted,
        )

    def put_tool(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        schema_ref: str | None = None,
        role: str | None = None,
        attempt_closed: bool = False,
        context_tainted: bool = False,
    ) -> Artifact:
        """Web/tool output — always ``tainted=True`` (DESIGN_M1 §8)."""
        return self.put(
            content,
            produced_by,
            name=name,
            media_type=media_type,
            schema_ref=schema_ref,
            role=role,
            attempt_closed=attempt_closed,
            tainted=True,
            context_tainted=context_tainted,
        )

    def put_fetched(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        capability: object,
        schema_ref: str | None = None,
        role: str | None = None,
        attempt_closed: bool = False,
        context_tainted: bool = False,
        expected_tool: str,
        expected_call_id: str,
        expected_study_id: str,
        expected_attempt_id: str,
    ) -> Artifact:
        """Fetch-tool path — ``producer=tool``, always ``tainted=True``.

        Requires a ``FetchCapability`` minted by ``invoke_tool`` for an
        ``is_fetch`` tool (not any read tool). Pair with ``SourceFetched``.
        Journal provenance ≠ content trusted: network bytes stay tainted;
        ``quote_in_source`` may still accept them when the digest is journaled.
        ``expected_*`` binds are required; assert (no consume) so append can
        consume the opaque id once.
        """
        from coscientist.core.source_fetch import assert_fetch_capability_bound

        assert_fetch_capability_bound(
            capability,
            tool=expected_tool,
            call_id=expected_call_id,
            study_id=expected_study_id,
            attempt_id=expected_attempt_id,
        )
        return self._store(
            content,
            produced_by,
            name=name,
            media_type=media_type,
            schema_ref=schema_ref,
            role=role,
            attempt_closed=attempt_closed,
            producer="tool",
            tainted=True,
            context_tainted=context_tainted,
        )

    def put_core(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        schema_ref: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        attempt_closed: bool = False,
        context_tainted: bool = False,
        tainted: bool = False,
    ) -> Artifact:
        """Core-owned write (trusted for verdict evidence)."""
        return self._store(
            content,
            produced_by,
            name=name,
            media_type=media_type,
            schema_ref=schema_ref,
            role=role,
            kind=kind,
            attempt_closed=attempt_closed,
            producer="core",
            tainted=tainted,
            context_tainted=context_tainted,
        )

    def put_sandbox(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        schema_ref: str | None = None,
        role: str | None = None,
        attempt_closed: bool = False,
        net_isolation: NetIsolation = "none",
        fs_isolation: FsIsolation = "none",
        context_tainted: bool = False,
        tainted: bool = False,
    ) -> Artifact:
        """Sandbox / verifier write. Context taint propagates to the output."""
        return self._store(
            content,
            produced_by,
            name=name,
            media_type=media_type,
            schema_ref=schema_ref,
            role=role,
            attempt_closed=attempt_closed,
            producer="sandbox",
            net_isolation=net_isolation,
            fs_isolation=fs_isolation,
            tainted=tainted,
            context_tainted=context_tainted,
        )

    def get_meta(self, artifact_id: str) -> Artifact | None:
        if artifact_id in self._index:
            return self._index[artifact_id]
        path = self.meta / f"{artifact_id}.json"
        if not path.exists():
            return None
        art = Artifact.model_validate_json(path.read_text(encoding="utf-8"))
        self._index[artifact_id] = art
        return art

    def stamp_kind(self, artifact_id: str, kind: str | None) -> Artifact | None:
        """Core-only stamp of ``Artifact.kind`` (experiment_design / contradiction_map)."""
        if kind not in ("experiment_design", "contradiction_map", None):
            raise ArtifactRejected(f"invalid artifact kind: {kind!r}")
        art = self.get_meta(artifact_id)
        if art is None:
            return None
        if art.kind == kind:
            return art
        updated = art.model_copy(update={"kind": kind})
        self._index[artifact_id] = updated
        (self.meta / f"{artifact_id}.json").write_text(
            updated.model_dump_json(), encoding="utf-8"
        )
        return updated

    def read(self, artifact_id: str) -> bytes:
        art = self.get_meta(artifact_id)
        if art is None:
            raise FileNotFoundError(artifact_id)
        data = self.read_cas(art.sha256)
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


def artifact_producer_trusted(
    meta: object | None,
) -> bool:
    """True when producer is core, or sandbox with both net and fs isolation hard."""
    if meta is None:
        return False
    producer = getattr(meta, "producer", "agent")
    if producer == "core":
        return True
    if producer == "sandbox":
        return (
            getattr(meta, "net_isolation", None) == "hard"
            and getattr(meta, "fs_isolation", None) == "hard"
        )
    return False


def output_kind_from_spec(
    *,
    role: str | None,
    media_type: str | None,
) -> Literal["code", "data"]:
    """Core kind from plan OutputSpec (role preferred; else media_type)."""
    if role == "code":
        return "code"
    if role in ("data", "paper", "figure", "table", "log", "report"):
        return "data"
    return "code" if _infer_artifact_role(media_type or "") == "code" else "data"
