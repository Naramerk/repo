"""Content-addressed artifact store with sha256 verify on read (DESIGN §3.8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.errors import ArtifactCorrupt, ArtifactRejected
from coscientist.core.execution.domain import (
    Artifact,
    ArtifactKind,
    ArtifactProducer,
    ArtifactRole,
    FsIsolation,
    NetIsolation,
    ToolCallRef,
)
from coscientist.core.execution.taint import derived_tainted

_VALID_ROLES: frozenset[str] = frozenset(
    {"paper", "data", "code", "figure", "table", "log", "report"}
)


def _require_role(role: str | ArtifactRole | None) -> ArtifactRole:
    if role is None:
        raise ArtifactRejected("artifact_role_required")
    if role not in _VALID_ROLES:
        raise ArtifactRejected(f"unknown_artifact_role:{role!r}")
    return cast(ArtifactRole, role)


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
        role: ArtifactRole,
        schema_ref: str | None = None,
        kind: str | None = None,
        attempt_closed: bool = False,
        net_isolation: NetIsolation | None = None,
        fs_isolation: FsIsolation | None = None,
        tainted: bool = False,
        context_tainted: bool = False,
        user_put: object | None = None,
    ) -> Artifact:
        if attempt_closed:
            raise ArtifactRejected("attempt_closed")
        if producer == "user":
            # Module-level verify (exact type + hmac) — not duck-typed is_live().
            from coscientist.core.execution._attempt_frame import attempt_active
            from coscientist.core.execution._ingest_gate import verify_user_put_capability

            if not verify_user_put_capability(user_put):
                raise ArtifactRejected("user_put_capability_required")
            if attempt_active():
                raise ArtifactRejected("put_user_requires_ingest_scope")
        digest = self.put_cas(content, tmp_name=produced_by.call_id)
        call_part = sha256_hex(produced_by.call_id)[:8]
        artifact_id = f"art_{digest[:16]}_{call_part}"
        stamped_role = _require_role(role)
        # kind is core-stamped only (experiment_design / contradiction_map).
        stamped_kind = cast(
            ArtifactKind | None,
            kind if kind in ("experiment_design", "contradiction_map") else None,
        )
        art = Artifact(
            artifact_id=artifact_id,
            name=name,
            media_type=media_type,
            sha256=digest,
            size=len(content),
            uri=f"file://{self.cas / digest}",
            produced_by=produced_by,
            schema_ref=schema_ref,
            role=stamped_role,
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
        role: ArtifactRole,
        schema_ref: str | None = None,
        kind: str | None = None,
        attempt_closed: bool = False,
        tainted: bool = False,
        context_tainted: bool = False,
    ) -> Artifact:
        """Agent-controlled path — always ``producer=agent`` (never trusted).

        ``kind`` is ignored here (agent cannot stamp experiment_design); use
        ``put_core`` / executor OutputSpec stamping. ``role`` is required.
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
        role: ArtifactRole,
        schema_ref: str | None = None,
        attempt_closed: bool = False,
        context_tainted: bool = False,
    ) -> Artifact:
        """Web/tool output — always ``tainted=True`` (DESIGN_M1 §8)."""
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

    def put_fetched(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
        role: ArtifactRole,
        capability: object,
        schema_ref: str | None = None,
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
        from coscientist.core.execution.source_fetch import assert_fetch_capability_bound

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
        role: ArtifactRole,
        schema_ref: str | None = None,
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
        role: ArtifactRole,
        schema_ref: str | None = None,
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

    def read(self, artifact_id: str) -> bytes:
        art = self.get_meta(artifact_id)
        if art is None:
            raise FileNotFoundError(artifact_id)
        data = self.read_cas(art.sha256)
        return data

    def read_json(self, artifact_id: str) -> object:
        return json.loads(self.read(artifact_id))


def make_tool_ref(
    call_id: str,
    tool: str = "local",
    args: str = "",
    *,
    attempt_id: str | None = None,
) -> ToolCallRef:
    return ToolCallRef(
        call_id=call_id,
        tool=tool,
        args_digest=sha256_hex(args),
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        attempt_id=attempt_id,
    )


def artifact_producer_trusted(
    meta: object | None,
) -> bool:
    """True when producer is core, or sandbox with both net and fs isolation hard."""
    if meta is None:
        return False
    try:
        producer = meta.producer  # type: ignore[attr-defined]
    except AttributeError as e:
        raise ValueError("artifact meta missing required producer") from e
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
    role: ArtifactRole,
) -> Literal["code", "data"]:
    """Core kind from plan OutputSpec.role (required; never inferred from media_type)."""
    if role == "code":
        return "code"
    if role in ("data", "paper", "figure", "table", "log", "report"):
        return "data"
    raise ValueError(f"unknown OutputSpec.role: {role!r}")
