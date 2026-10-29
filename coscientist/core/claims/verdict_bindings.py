"""Binding / artifact provenance helpers for independence digests (PR8b).

Extracted from ``verdict`` — code-vs-data walk, root data digests, catalog
output kinds. Callers import from this module (no re-export on ``verdict``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from coscientist.core.events.digest import canonical_json
from coscientist.core.finalize.soft_failures import FINALIZE_SOFT_FAILURES

_MAX_PROVENANCE_DEPTH = 64

ArtifactLookup = Callable[[str], object | None]
ResultPayload = Mapping[str, object]


def _media_type_is_code(media_type: str | None) -> bool:
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
        return True
    return mt.startswith("text/x-")


def artifact_is_code(meta: object | None) -> bool:
    """Legacy meta heuristic (role / media_type). Prefer ``output_kinds``.

    Unknown → DATA. Never uses InputBinding slot names.
    """
    if meta is None:
        return False
    # Artifact + test doubles; role/media_type may be absent on partial metas.
    role = getattr(meta, "role", None)
    if role == "code":
        return True
    if role in ("data", "paper", "figure", "table", "log", "report"):
        return False
    return _media_type_is_code(getattr(meta, "media_type", None))


def binding_is_code(
    input_name: str | None = None,
    *,
    code_output_names: Sequence[str] | None = None,
    meta: object | None = None,
) -> bool:
    """Deprecated: use ``artifact_is_code(meta)``. Slot names are ignored."""
    del input_name, code_output_names
    return artifact_is_code(meta)


def output_kinds_from_catalog(
    *,
    output_names: Sequence[str] | None,
    catalog: object | None,
) -> dict[str, str]:
    """name → code|data from catalog ``code_output_names`` (ignore planner role).

    Names listed in the catalog are code; everything else defaults to **data**.
    Planner ``OutputSpec.role`` / media_type are ignored for this decision.
    """
    # Catalog + test doubles; missing attr matches prior getattr(..., None) or ().
    raw = getattr(catalog, "code_output_names", None) if catalog is not None else None
    code_names = {str(x) for x in (raw or ()) if x}
    out: dict[str, str] = {}
    for name in output_names or ():
        n = str(name or "")
        if not n:
            continue
        out[n] = "code" if n in code_names else "data"
    return out


def output_kinds_from_task_spec(
    spec: object | None,
    catalog: object | None = None,
) -> dict[str, str]:
    """Backward-compat wrapper: kinds from catalog names on plan outputs."""
    names: list[str] = []
    if spec is not None:
        # TaskSpec.expected_outputs; missing attr matches prior getattr path.
        for ospec in getattr(spec, "expected_outputs", None) or ():
            n = str(getattr(ospec, "name", "") or "")
            if n:
                names.append(n)
    return output_kinds_from_catalog(output_names=names, catalog=catalog)


def _kind_from_producer(
    artifact_id: str,
    producer: ResultPayload | None,
) -> Literal["code", "data"] | None:
    """Core ``output_kinds`` for this artifact if the producer recorded them."""
    if producer is None:
        return None
    kinds = producer.get("output_kinds")
    if not isinstance(kinds, Mapping) or not kinds:
        return None
    omap = producer.get("output_map")
    if not isinstance(omap, Mapping):
        return None
    for name, aid in omap.items():
        if aid and str(aid) == artifact_id and name in kinds:
            k = kinds[name]
            return "code" if k == "code" else "data"
    # Produced but no OutputSpec entry → DATA (never agent media_type).
    return "data"


def _walk_artifact_is_code(
    artifact_id: str,
    *,
    producers: Mapping[str, ResultPayload],
    artifact_lookup: ArtifactLookup | None,
    notes: list[str],
) -> bool:
    """Code vs data: ``ResultRecorded.output_kinds`` wins; else legacy meta."""
    kind = _kind_from_producer(artifact_id, producers.get(artifact_id))
    if kind is not None:
        return kind == "code"
    meta = _safe_get_meta(
        artifact_id, artifact_lookup=artifact_lookup, notes=notes
    )
    return artifact_is_code(meta)


def _binding_fields(b: object) -> tuple[str, str, str | None, str | None]:
    """kind, input_name, artifact_id, binding digest (optional fallback)."""
    if isinstance(b, Mapping):
        return (
            str(b.get("kind", "artifact")),
            str(b.get("input") or ""),
            str(b.get("artifact_id") or "") or None,
            str(b.get("digest") or "") or None,
        )
    # InputBinding / duck object — defaults match prior getattr totality path.
    return (
        str(getattr(b, "kind", "artifact")),
        str(getattr(b, "input", "") or ""),
        str(getattr(b, "artifact_id", "") or "") or None,
        str(getattr(b, "digest", "") or "") or None,
    )


def _safe_get_meta(
    artifact_id: str,
    *,
    artifact_lookup: ArtifactLookup | None,
    notes: list[str],
) -> object | None:
    if not artifact_id or artifact_lookup is None:
        return None
    try:
        return artifact_lookup(artifact_id)
    except FINALIZE_SOFT_FAILURES as exc:
        notes.append(f"get_meta:{artifact_id}:{type(exc).__name__}")
        return None


def _content_digest(
    artifact_id: str,
    *,
    artifact_lookup: ArtifactLookup | None,
    fallback: str | None = None,
    notes: list[str] | None = None,
) -> str | None:
    note_buf = notes if notes is not None else []
    meta = _safe_get_meta(
        artifact_id, artifact_lookup=artifact_lookup, notes=note_buf
    )
    if meta is not None:
        # Artifact.sha256; some test doubles expose ``digest`` instead.
        d = getattr(meta, "sha256", None) or getattr(meta, "digest", None)
        if d:
            return str(d)
    return str(fallback) if fallback else None


def _producer_index(
    results: Mapping[str, ResultPayload] | None,
    *,
    notes: list[str] | None = None,
) -> dict[str, ResultPayload]:
    """artifact_id → ResultRecorded payload (first writer wins). Never raises."""
    idx: dict[str, ResultPayload] = {}
    note_buf = notes if notes is not None else []
    if not results:
        return idx
    try:
        seen_payloads: set[int] = set()
        for _key, payload in results.items():
            try:
                if not isinstance(payload, Mapping):
                    continue
                pid = id(payload)
                if pid in seen_payloads:
                    continue
                seen_payloads.add(pid)
                arts = payload.get("artifacts")
                if isinstance(arts, list | tuple):
                    for aid in arts:
                        idx.setdefault(str(aid), payload)
                omap = payload.get("output_map")
                if isinstance(omap, Mapping):
                    for aid in omap.values():
                        if aid:
                            idx.setdefault(str(aid), payload)
            except FINALIZE_SOFT_FAILURES as exc:
                note_buf.append(f"producer_index:{type(exc).__name__}")
    except FINALIZE_SOFT_FAILURES as exc:
        note_buf.append(f"producer_index:{type(exc).__name__}")
    return idx


def root_data_digests_from_bindings(
    bindings: Sequence[object] | None,
    *,
    results: Mapping[str, ResultPayload] | None = None,
    artifact_lookup: ArtifactLookup | None = None,
    code_output_names: Sequence[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Content digests of **root** data artifacts reachable from bindings.

    Walks InputBinding → producer ResultRecorded.inputs (depth-guarded DFS).
    Code vs data from core ``ResultRecorded.output_kinds`` (plan OutputSpec);
    agent ``media_type`` is ignored when kinds are present. Diamonds reuse a
    ``done`` set; only true cycles (``on_stack``) are noted. An artifact with
    no upstream producer, or whose upstream inputs are all code, is a root.
    Unknown kind → DATA.

    Returns ``(sorted_root_digests, error_notes)``. Never raises.
    """
    del code_output_names  # slot-name catalog lists are not used for this decision
    notes: list[str] = []
    producers = _producer_index(results, notes=notes)
    roots: set[str] = set()

    def _walk(
        artifact_id: str,
        fallback: str | None,
        depth: int,
        on_stack: set[str],
        done: set[str],
    ) -> None:
        if not artifact_id:
            return
        if artifact_id in done:
            return
        if artifact_id in on_stack:
            notes.append(f"cycle:{artifact_id}")
            return
        if depth > _MAX_PROVENANCE_DEPTH:
            notes.append(f"depth:{artifact_id}")
            d = _content_digest(
                artifact_id,
                artifact_lookup=artifact_lookup,
                fallback=fallback,
                notes=notes,
            )
            if d:
                roots.add(d)
            done.add(artifact_id)
            return

        on_stack.add(artifact_id)
        try:
            if _walk_artifact_is_code(
                artifact_id,
                producers=producers,
                artifact_lookup=artifact_lookup,
                notes=notes,
            ):
                return

            producer = producers.get(artifact_id)
            if producer is None:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            raw_inputs = producer.get("inputs")
            input_rows: Sequence[object] = (
                raw_inputs if isinstance(raw_inputs, list | tuple) else ()
            )
            upstream = [
                b
                for b in input_rows
                if _binding_fields(b)[0] == "artifact" and _binding_fields(b)[2]
            ]
            if not upstream:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            data_children: list[tuple[str, str | None]] = []
            for b in upstream:
                _k, _n, child, cdig = _binding_fields(b)
                if not child:
                    continue
                if _walk_artifact_is_code(
                    child,
                    producers=producers,
                    artifact_lookup=artifact_lookup,
                    notes=notes,
                ):
                    continue
                data_children.append((child, cdig))

            if not data_children:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            for child, cdig in data_children:
                _walk(child, cdig, depth + 1, on_stack, done)
        finally:
            on_stack.discard(artifact_id)
            done.add(artifact_id)

    on_stack: set[str] = set()
    done: set[str] = set()
    for b in bindings or ():
        kind, _iname, aid, bdig = _binding_fields(b)
        if kind != "artifact" or not aid:
            continue
        _walk(aid, bdig, 0, on_stack, done)

    return sorted(roots), notes


def data_digests_from_bindings(
    bindings: Sequence[object] | None,
    *,
    artifact_lookup: ArtifactLookup | None = None,
    code_output_names: Sequence[str] | None = None,
    results: Mapping[str, ResultPayload] | None = None,
) -> list[str]:
    """Root data digests for independence (artifact meta; transitive walk)."""
    digs, _notes = root_data_digests_from_bindings(
        bindings,
        results=results,
        artifact_lookup=artifact_lookup,
        code_output_names=code_output_names,
    )
    return digs


def digests_from_bindings(
    bindings: Sequence[object] | None,
    *,
    artifact_lookup: ArtifactLookup | None = None,
    code_output_names: Sequence[str] | None = None,
    results: Mapping[str, ResultPayload] | None = None,
) -> tuple[str | None, str | None]:
    """Deprecated shape: (folded_data, None). Prefer ``data_digests_from_bindings``."""
    data = data_digests_from_bindings(
        bindings,
        artifact_lookup=artifact_lookup,
        code_output_names=code_output_names,
        results=results,
    )
    if not data:
        return None, None
    if len(data) == 1:
        return data[0], None
    return hashlib.sha256(canonical_json(data)).hexdigest(), None
