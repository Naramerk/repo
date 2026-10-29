"""Runtime claim projection for verdict() / dispositions (M1 PR8)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from coscientist.core.claims.graph import GraphStore
from coscientist.core.claims.rules import priority_refuted_support_note
from coscientist.core.claims.stance import DEFAULT_TESTS_INTENT, TestsIntent, claim_stance
from coscientist.core.events.ids import (
    claim_node_id,
    experiment_node_id,
    hypothesis_node_id,
    question_node_id,
)
from coscientist.core.execution.domain import (
    VALID_PROVENANCE,
    Provenance,
    Study,
    worst_provenance,
)
from coscientist.core.finalize.soft_failures import FINALIZE_SOFT_FAILURES

# Agent-supplied fields only. Counting controls (criterion_status, frozen,
# evidence_kind, lit_own_criterion, intent, R17, accepted) are core-derived.
CLAIM_ATTR_WHITELIST: frozenset[str] = frozenset(
    {
        "target",
        "checked_against",
        "reason",
        "premise_idx",
    }
)

# Core-owned attrs written by Executor (never agent whitelist).
_CORE_CLAIM_ATTRS: frozenset[str] = frozenset(
    {
        "provenance",
        "attempt_id",
        "attempt_outcome",
        "criterion_status",
        "evidence_kind",
        "frozen",
        "lit_own_criterion",
        "accepted",
        "artifact_id",
        "validity_ok",
        "check_role",
        "exclusion_reason",
        "claim_outcome",
        "severe",
        "independence_key",
        "experiment_id",
        "refute_capable",
        "p_value",
        "power",
        "sesoi",
        "uses_sesoi",
        "used_sesoi",
        "predicts_null",
        "predicted_direction",
        "data_digests",
        "criterion_template_ids",
        "study_id",
        "effect",
        "alpha",
        "refute_unresolved",
        "timeout_kind",
        "derivation_executable",
    }
)

_VERDICT_TO_DISPOSITION = {
    "supported": "answered",
    "refuted": "refuted",
    "survived": "survived",
    "inconclusive": "inconclusive",
    "untestable_by_agent": "infeasible",
}


@dataclass
class RuntimeClaim:
    """Minimal claim surface for ``hypothesis_verdict`` / R17."""

    criterion_status: str = "unassessed"
    accepted: bool = False
    evidence_kind: str = "computational"
    provenance: str = "stub"
    frozen: bool = False
    attempt_outcome: str = "SUCCESS"
    attempt_id: str = ""
    artifact_id: str = ""
    lit_own_criterion: bool = False
    # M1 PR15: derivation with executable CodeCheck MET lifts inconclusive ceiling.
    derivation_executable: bool = False
    intent: TestsIntent = DEFAULT_TESTS_INTENT
    criterion_frozen_at_seq: int | None = None
    first_cited_evidence_seq: int | None = None
    confirming_evidence_ids: set[str] | frozenset[str] | list[str] | None = None
    threshold_source_evidence_ids: set[str] | frozenset[str] | list[str] | None = None
    target: str = ""
    target_kind: Literal["question", "hypothesis"] = "question"
    node_id: str = ""
    # Duhem–Quine: False when validity failed or role is unknown on this attempt.
    validity_ok: bool = True
    check_role: str = "unknown"
    exclusion_reason: str | None = None
    # PR8b claim classification (DESIGN_HYP_DELTA).
    claim_outcome: str | None = None  # consistent|inconsistent|indeterminate|None
    severe: bool | None = None
    independence_key: str = ""
    experiment_id: str = ""
    refute_capable: bool | None = None
    p_value: float | None = None
    power: float | None = None
    sesoi: float | None = None
    uses_sesoi: bool | None = None
    used_sesoi: bool | None = None
    predicts_null: bool = False
    predicted_direction: str = "two_sided"
    data_digests: list[str] | None = None
    criterion_template_ids: list[str] | None = None
    study_id: str = ""
    effect: dict[str, Any] | None = None
    validity: str | None = None  # valid|invalid (mirrors validity_ok)
    # Guest sandbox timeout closed a refute-role check without a decisive outcome.
    refute_unresolved: bool = False
    timeout_kind: str | None = None  # idle_timeout|cpu_timeout|wall_timeout

    def __post_init__(self) -> None:
        if self.intent not in ("confirm", "refute"):
            self.intent = DEFAULT_TESTS_INTENT

    @property
    def group_key(self) -> str:
        """Kind-prefixed target so Q and H with the same logical id do not merge."""
        return f"{self.target_kind}:{self.target}"

    @property
    def stance(self) -> str:
        try:
            return claim_stance(self.criterion_status)
        except KeyError:
            return "inconclusive"

    @property
    def is_lit(self) -> bool:
        return self.evidence_kind == "literature"

    @property
    def is_derivation(self) -> bool:
        return self.evidence_kind == "derivation"

    def counts(self) -> bool:
        """PR8b/PR9/PR15 counting filter: real ∧ valid ∧ consistent|inconsistent.

        Stubs/partials are excluded **before** counting (no provenance ceiling).
        Literature never counts. Review-only derivation never counts (DESIGN_M1
        §6.6); only derivation with a real executable CodeCheck MET feeds
        k_sup/k_ref. Validity failures never count. PR9/C10: claims whose
        target (or checked_against) is superseded are out.
        """
        if self.exclusion_reason in ("superseded", "unapproved_check"):
            return False
        if not (self.accepted and self.validity_ok and self.frozen):
            return False
        if self.provenance != "real":
            return False
        if self.is_lit:
            return False
        if self.evidence_kind not in ("computational", "experimental", "derivation"):
            return False
        if self.is_derivation and not self.derivation_executable:
            return False
        from coscientist.core.claims.verdict import effective_outcome

        return effective_outcome(self) in ("consistent", "inconsistent")


def whitelist_claim_attrs(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only allowed agent fields (never stance / counting controls)."""
    return {k: v for k, v in raw.items() if k in CLAIM_ATTR_WHITELIST}


_NONCOUNT_RESULTS = frozenset({"infra", "core_bug"})
_INFRA_REASONS = frozenset(
    {
        "catalog_error",
        "crash",
        "invalid_output",
        "timeout",
        "human_timeout",
        "core_bug",
        "sandbox_infra",
    }
)


def producer_attempt_failed(
    attempt_id: str | None,
    *,
    results: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    """True when the producing attempt is infra/core_bug (not criteria).

    Scientific FAILED (criteria not met) still yields bindable claims.
    ``result_class=core_bug`` is reported but never counts.
    """
    if not attempt_id or not results:
        return False
    row = results.get(attempt_id)
    if not row:
        return False
    rc = str(row.get("result_class") or "")
    if rc in _NONCOUNT_RESULTS:
        return True
    # Scientific criteria + guest timeout keep claims (refute_unresolved / attrs).
    if rc in ("scientific", "timeout"):
        return False
    # Legacy journals without result_class: FAILED + infra reason only.
    if str(row.get("outcome") or "") != "FAILED":
        return False
    reason = str(row.get("reason") or "")
    return reason in _INFRA_REASONS


def _coerce_str(val: Any, *, allow_empty: bool = False) -> str | None:
    if val is None:
        return "" if allow_empty else None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s and not allow_empty:
        return None
    return s


def _coerce_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    return None


def _coerce_optional_int(val: Any) -> int | None | Literal[False]:
    """None if absent; int if valid; False if present but garbage."""
    if val is None:
        return None
    if isinstance(val, bool):
        return False
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return False


def _coerce_id_set(val: Any) -> frozenset[str] | None | Literal[False]:
    """None if absent; frozenset if valid; False if present but garbage."""
    if val is None:
        return None
    if isinstance(val, (str, bytes, bytearray, Mapping)):
        return False
    if isinstance(val, (int, float, bool)):
        return False
    try:
        return frozenset(str(x) for x in val)
    except TypeError:
        return False


def _task_key_from_attempt_id(attempt_id: str) -> str | None:
    """``{study}/{task_key}#{n}`` → ``task_key``."""
    if not attempt_id or "#" not in attempt_id:
        return None
    left = attempt_id.rsplit("#", 1)[0]
    if "/" not in left:
        return None
    return left.rsplit("/", 1)[-1]


def intent_from_producing_experiment(
    graph: GraphStore | None,
    *,
    study_id: str,
    attempt_id: str,
    target_logical: str,
    target_kind: str,
) -> TestsIntent:
    """Read ``tests.intent`` from the producing experiment's tests edge.

    Path: attempt_id → task_key → experiment node → that experiment's
    ``tests`` edge to the hypothesis (DESIGN_M1 §3). Falls back to confirm.
    """
    if graph is None or target_kind != "hypothesis" or not target_logical:
        return DEFAULT_TESTS_INTENT
    task_key = _task_key_from_attempt_id(attempt_id)
    if not task_key:
        return DEFAULT_TESTS_INTENT
    exp_id = experiment_node_id(study_id, task_key)
    hid = (
        target_logical
        if target_logical.startswith(f"{study_id}:hypothesis:")
        else hypothesis_node_id(study_id, target_logical)
    )
    for e in graph.edges_from(exp_id):
        if e.kind != "tests" or e.dst_id != hid:
            continue
        raw = (e.attrs or {}).get("intent", DEFAULT_TESTS_INTENT)
        if raw in ("confirm", "refute"):
            return raw  # type: ignore[return-value]
    return DEFAULT_TESTS_INTENT


def intent_from_tests_edge(
    graph: GraphStore | None,
    *,
    study_id: str,
    target_logical: str,
    target_kind: str,
    attempt_id: str = "",
) -> TestsIntent:
    """Backward-compatible wrapper; prefer producing-experiment intent."""
    if attempt_id:
        return intent_from_producing_experiment(
            graph,
            study_id=study_id,
            attempt_id=attempt_id,
            target_logical=target_logical,
            target_kind=target_kind,
        )
    # Legacy: first tests→hypothesis edge (tests only; prefer attempt_id path).
    if graph is None or target_kind != "hypothesis" or not target_logical:
        return DEFAULT_TESTS_INTENT
    hid = (
        target_logical
        if target_logical.startswith(f"{study_id}:hypothesis:")
        else hypothesis_node_id(study_id, target_logical)
    )
    for e in graph.all_edges():
        if e.kind != "tests" or e.dst_id != hid:
            continue
        raw = (e.attrs or {}).get("intent", DEFAULT_TESTS_INTENT)
        if raw in ("confirm", "refute"):
            return raw  # type: ignore[return-value]
    return DEFAULT_TESTS_INTENT


def resolve_existing_target(
    study_id: str,
    target: str,
    graph: GraphStore | None,
) -> tuple[str, Literal["question", "hypothesis"]] | None:
    """Map ``target`` to an **existing** graph node; never create / never H* guess."""
    if not target or graph is None:
        return None
    if target.startswith(f"{study_id}:question:"):
        if graph.get_node(target) is not None:
            return target, "question"
        return None
    if target.startswith(f"{study_id}:hypothesis:"):
        if graph.get_node(target) is not None:
            return target, "hypothesis"
        return None
    qid = question_node_id(study_id, target)
    if graph.get_node(qid) is not None:
        return qid, "question"
    hid = hypothesis_node_id(study_id, target)
    if graph.get_node(hid) is not None:
        return hid, "hypothesis"
    return None


def namespaced_claim_id(study_id: str, attempt_id: str, agent_key: str) -> str | None:
    """Claim ids are namespaced per attempt; foreign full ids are rejected."""
    key = str(agent_key)
    prefix = f"{study_id}:claim:"
    ns = f"{prefix}{attempt_id}:"
    if key.startswith(prefix):
        if key.startswith(ns):
            return key
        return None  # foreign / unscoped agent-chosen full id
    # Short key → namespace under this attempt.
    safe = key.replace("/", "_")
    return claim_node_id(study_id, f"{attempt_id}:{safe}")


def runtime_claim_from_node(
    node: Any,
    *,
    study_id: str,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    graph: GraphStore | None = None,
    artifact_lookup: Any | None = None,
) -> tuple[RuntimeClaim | None, str | None]:
    """Build a RuntimeClaim; on garbage attrs return ``(None, reason)`` — never raise.

    When ``artifact_lookup`` is provided, effect ``trusted``/``countable`` are
    recomputed from live artifact metadata. Journal-only replay falls back to
    effect-stamped provenance facts written at eval from that same meta.
    """
    try:
        attrs = dict(node.attrs or {})
        aid = _coerce_str(attrs.get("attempt_id"), allow_empty=True) or ""
        outcome = _coerce_str(attrs.get("attempt_outcome"), allow_empty=True) or ""
        if results and aid and aid in results:
            outcome = str(results[aid].get("outcome") or outcome)
        # Drop only infra failures; scientific FAILED (criteria) still counts.
        if producer_attempt_failed(aid, results=results):
            return None, "producer_failed"
        if not outcome:
            outcome = "SUCCESS"

        prov = attrs.get("provenance")
        if prov not in VALID_PROVENANCE:
            prov = "stub"

        # Core-derived counting fields (not agent whitelist).
        status = _coerce_str(attrs.get("criterion_status"))
        if status is None or status not in ("met", "not_met", "unassessed"):
            return None, "bad_criterion_status"
        accepted = _coerce_bool(attrs.get("accepted"))
        if accepted is None:
            return None, "bad_accepted"
        # Absent on pre-role journals → treat as ok (prediction-era claims).
        validity_ok_raw = attrs.get("validity_ok")
        if validity_ok_raw is None:
            validity_ok = True
        else:
            coerced_vok = _coerce_bool(validity_ok_raw)
            if coerced_vok is None:
                return None, "bad_validity_ok"
            validity_ok = coerced_vok
        check_role = _coerce_str(attrs.get("check_role"), allow_empty=True) or "unknown"
        if check_role not in ("prediction", "validity", "unknown"):
            check_role = "unknown"
        excl_raw = _coerce_str(attrs.get("exclusion_reason"), allow_empty=True)
        _EXCL = (
            "validity_failed",
            "unknown_role",
            "stub",
            "partial",
            "invalid",
            "indeterminate",
            "lit",
            "r17",
            "superseded",
            "foreign_artifact",
            "output_not_submitted",
            "artifact_missing",
            "pointer_empty",
        )
        exclusion_reason = excl_raw if excl_raw in _EXCL else None
        claim_outcome_raw = _coerce_str(attrs.get("claim_outcome"), allow_empty=True) or (
            _coerce_str(attrs.get("outcome"), allow_empty=True)
        )
        claim_outcome = (
            claim_outcome_raw
            if claim_outcome_raw in ("consistent", "inconsistent", "indeterminate")
            else None
        )
        # Never trust stored severe/refute_capable — recompute after attrs load.
        # Absent severe on legacy journals ⇒ treat as False until recomputed (V5).
        independence_key = (
            _coerce_str(attrs.get("independence_key"), allow_empty=True) or ""
        )
        experiment_id = (
            _coerce_str(attrs.get("experiment_id"), allow_empty=True) or ""
        )
        if not independence_key:
            from coscientist.core.claims.verdict import independence_key_for

            # Legacy: key from experiment_id (task), never attempt_id (retries).
            legacy_exp = experiment_id
            if not legacy_exp:
                aid = _coerce_str(attrs.get("attempt_id"), allow_empty=True) or ""
                legacy_exp = aid.rsplit("#", 1)[0] if aid else node.node_id
            independence_key = independence_key_for(experiment_id=legacy_exp)
        p_value = attrs.get("p_value")
        if p_value is not None:
            try:
                p_value = float(p_value)
            except (TypeError, ValueError):
                p_value = None
        power = attrs.get("power")
        if power is not None:
            try:
                power = float(power)
            except (TypeError, ValueError):
                power = None
        sesoi = attrs.get("sesoi")
        if sesoi is not None:
            try:
                sesoi = float(sesoi)
            except (TypeError, ValueError):
                sesoi = None
        uses_sesoi = _coerce_bool(attrs.get("uses_sesoi"))
        used_sesoi = _coerce_bool(attrs.get("used_sesoi"))
        predicts_null = bool(attrs.get("predicts_null", False))
        pd_raw = attrs.get("predicted_direction")
        predicted_direction = (
            str(pd_raw)
            if pd_raw in ("positive", "negative", "two_sided")
            else "two_sided"
        )
        raw_digests = attrs.get("data_digests")
        data_digests: list[str] | None
        if isinstance(raw_digests, list):
            data_digests = [str(x) for x in raw_digests if x]
        else:
            data_digests = None
        raw_tmpls = attrs.get("criterion_template_ids")
        criterion_template_ids: list[str] | None
        if isinstance(raw_tmpls, list):
            criterion_template_ids = [str(x) for x in raw_tmpls if x]
        else:
            criterion_template_ids = None
        study_id_attr = _coerce_str(attrs.get("study_id"), allow_empty=True) or ""
        effect_raw = attrs.get("effect")
        effect: dict[str, Any] | None
        if isinstance(effect_raw, dict):
            from coscientist.core.claims.verdict import recompute_effect_provenance

            # Prefer live artifact meta; else effect-stamped provenance facts.
            meta = None
            art_id = effect_raw.get("artifact_id") or attrs.get("artifact_id")
            if artifact_lookup is not None and art_id:
                try:
                    meta = artifact_lookup(str(art_id))
                except FINALIZE_SOFT_FAILURES:
                    meta = None
            stamped = dict(effect_raw)
            # Never invent produced_by_call_id from the attempt id alone — a bare
            # aid is not a call_id; missing call_id stays missing (not countable).
            effect = recompute_effect_provenance(
                stamped,
                attempt_id=aid or None,
                artifact_meta=meta,
            )
        else:
            effect = None
        frozen = _coerce_bool(attrs.get("frozen"))
        if frozen is None:
            return None, "bad_frozen"
        ek = _coerce_str(attrs.get("evidence_kind"))
        from coscientist.core.claims.derivation import VALID_EVIDENCE_KINDS

        if ek is None or ek not in VALID_EVIDENCE_KINDS:
            return None, "bad_evidence_kind"
        lit_own = _coerce_bool(attrs.get("lit_own_criterion"))
        if lit_own is None:
            lit_own = False

        conf = _coerce_id_set(attrs.get("confirming_evidence_ids"))
        if conf is False:
            return None, "bad_confirming_evidence_ids"
        thr = _coerce_id_set(attrs.get("threshold_source_evidence_ids"))
        if thr is False:
            return None, "bad_threshold_source_evidence_ids"
        frozen_seq = _coerce_optional_int(attrs.get("criterion_frozen_at_seq"))
        if frozen_seq is False:
            return None, "bad_criterion_frozen_at_seq"
        first_ev = _coerce_optional_int(attrs.get("first_cited_evidence_seq"))
        if first_ev is False:
            return None, "bad_first_cited_evidence_seq"

        target = _coerce_str(attrs.get("target"), allow_empty=True) or ""
        about = []
        target_kind = "question"
        if graph is not None:
            about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
            if len(about) != 1:
                return None, "about_cardinality"
            dst = about[0].dst_id
            dst_node = graph.get_node(dst)
            if dst_node is None:
                return None, "about_dst_missing"
            if dst_node.kind == "question":
                target_kind = "question"
                target = _logical_id_from_node(dst, study_id, "question")
            elif dst_node.kind == "hypothesis":
                target_kind = "hypothesis"
                target = _logical_id_from_node(dst, study_id, "hypothesis")
            else:
                return None, "about_dst_kind"

        # PR9/C10: superseded targets (and criteria) are excluded from k.
        if graph is not None and exclusion_reason is None:
            from coscientist.core.plan.supersede import (
                criterion_is_superseded,
                question_is_superseded,
            )

            if target_kind == "question" and question_is_superseded(graph, study_id, target):
                exclusion_reason = "superseded"
            else:
                checked = _coerce_str(attrs.get("checked_against"), allow_empty=True)
                if checked and criterion_is_superseded(graph, study_id, checked):
                    exclusion_reason = "superseded"

        intent = intent_from_producing_experiment(
            graph,
            study_id=study_id,
            attempt_id=aid or "",
            target_logical=target,
            target_kind=target_kind,
        )
        art = _coerce_str(attrs.get("artifact_id"), allow_empty=True) or node.node_id
        # Never trust stored severe/refute_capable=True (forged journals).
        # Absent or explicit False ⇒ never-severe (V5); True is ignored and recomputed.
        from coscientist.core.claims.verdict import (
            claim_is_severe,
            effect_has_interval,
            effect_is_countable,
        )

        sesoi_f = sesoi if isinstance(sesoi, float) else None
        stored_severe = attrs.get("severe")
        # Seed False when absent/explicit False so claim_is_severe short-circuits (V5).
        # Seed None when stored True so forgery cannot win without check evidence.
        probe_severe: bool | None
        if stored_severe is False or "severe" not in attrs:
            probe_severe = False
        else:
            probe_severe = None
        stored_refute = attrs.get("refute_capable")
        probe = RuntimeClaim(
            criterion_status=status,
            accepted=accepted,
            evidence_kind=ek,
            provenance=str(prov),
            frozen=frozen,
            attempt_outcome=outcome,
            attempt_id=aid or node.node_id,
            artifact_id=art,
            lit_own_criterion=lit_own,
            derivation_executable=bool(attrs.get("derivation_executable")),
            intent=intent,
            criterion_frozen_at_seq=frozen_seq,
            first_cited_evidence_seq=first_ev,
            confirming_evidence_ids=conf,
            threshold_source_evidence_ids=thr,
            target=target,
            target_kind=target_kind,  # type: ignore[arg-type]
            node_id=node.node_id,
            validity_ok=bool(validity_ok),
            check_role=check_role,
            exclusion_reason=exclusion_reason,
            claim_outcome=claim_outcome,
            severe=probe_severe,
            independence_key=independence_key,
            p_value=p_value if isinstance(p_value, float) else None,
            power=power if isinstance(power, float) else None,
            sesoi=sesoi_f,
            uses_sesoi=uses_sesoi,
            used_sesoi=used_sesoi,
            predicts_null=predicts_null,
            predicted_direction=predicted_direction,
            data_digests=data_digests,
            criterion_template_ids=criterion_template_ids,
            study_id=study_id_attr or study_id,
            effect=effect,
            experiment_id=experiment_id,
            refute_capable=False if stored_refute is False else None,
            refute_unresolved=bool(
                attrs.get("refute_unresolved")
                or (isinstance(effect, dict) and effect.get("refute_unresolved"))
            ),
            timeout_kind=(
                str(attrs["timeout_kind"])
                if attrs.get("timeout_kind")
                else (
                    str(effect.get("timeout_kind"))
                    if isinstance(effect, dict) and effect.get("timeout_kind")
                    else None
                )
            ),
        )
        severe_coerced = claim_is_severe(probe)
        if stored_refute is False:
            refute_capable = False
        else:
            # Recompute — ignore stored True; require countable evidence (null → CI).
            refute_capable = bool(
                frozen
                and check_role == "prediction"
                and uses_sesoi is True
                and sesoi_f is not None
                and used_sesoi is True
                and effect_is_countable(effect)
                and (effect_has_interval(effect) if predicts_null else True)
            )
        probe.severe = severe_coerced
        probe.refute_capable = refute_capable
        return probe, None
    except FINALIZE_SOFT_FAILURES:
        return None, "coerce_error"


def _logical_id_from_node(node_id: str, study_id: str, kind: str) -> str:
    prefix = f"{study_id}:{kind}:"
    if node_id.startswith(prefix):
        return node_id[len(prefix) :]
    return node_id


def filtered_claims_by_target(
    graph: GraphStore,
    *,
    study_id: str,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    artifact_lookup: Any | None = None,
) -> dict[str, list[RuntimeClaim]]:
    """Group claims by ``{kind}:{logical}``; about + coerce applied uniformly.

    Keys are kind-prefixed (``question:Q1``, ``hypothesis:H1``) so a question
    and hypothesis that share a logical id never merge. Used by both
    ``dispositions`` and ``compute_verdict_snapshots``.
    """
    grouped: dict[str, list[RuntimeClaim]] = defaultdict(list)
    for node in graph.nodes_by_kind("claim"):
        rc, _reason = runtime_claim_from_node(
            node,
            study_id=study_id,
            results=results,
            graph=graph,
            artifact_lookup=artifact_lookup,
        )
        if rc is None or not rc.target:
            continue
        grouped[rc.group_key].append(rc)
    return dict(grouped)


def parse_target_ref(
    ref: str,
) -> tuple[Literal["question", "hypothesis"] | None, str]:
    """Split ``question:Q1`` / ``hypothesis:H1``; bare logical → ``(None, ref)``."""
    if ":" in ref:
        kind, logical = ref.split(":", 1)
        if kind in ("question", "hypothesis") and logical:
            return kind, logical  # type: ignore[return-value]
    return None, ref


def resolve_verdict_ref_kind(
    graph: GraphStore | None,
    study_id: str,
    ref: str,
) -> Literal["question", "hypothesis"]:
    """Explicit target kind for a bind/snapshot ref (never ``either``).

    Kind-prefixed refs win. Otherwise unique graph presence; if both Q and H
    exist for the same logical id, default to ``question`` (bind addresses).
    """
    kind, logical = parse_target_ref(ref)
    if kind is not None:
        return kind
    if graph is None:
        return "question"
    q_exists = graph.get_node(question_node_id(study_id, logical)) is not None
    h_exists = graph.get_node(hypothesis_node_id(study_id, logical)) is not None
    if h_exists and not q_exists:
        return "hypothesis"
    return "question"


def claims_for_ref(
    by_target: Mapping[str, list[RuntimeClaim]],
    ref: str,
    *,
    prefer: Literal["question", "hypothesis"],
) -> list[RuntimeClaim]:
    """Lookup claims for a bind/report ref with an **explicit** target kind."""
    kind, logical = parse_target_ref(ref)
    use = kind if kind is not None else prefer
    return list(by_target.get(f"{use}:{logical}") or [])


def claims_missing_about(graph: GraphStore) -> list[str]:
    """Claim node ids that lack the required out-1 ``about`` edge."""
    missing: list[str] = []
    for node in graph.nodes_by_kind("claim"):
        about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
        if len(about) != 1:
            missing.append(node.node_id)
    return missing


def compute_verdict_snapshots(
    graph: GraphStore,
    *,
    study_id: str,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int | None = None,
    any_experiment_done: bool = False,
    refs: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """ref → snapshot dict used for ``InputSpec.kind=verdict`` P2 digests.

    ``k`` defaults to Study ``confirmations_needed`` (same as ``dispositions``).
    When ``refs`` is set, only those logical ids are computed (scoped bind).
    Output keys are the caller's refs (logical ids), not kind-prefixed.
    RC3t: ``study is None`` → typed refuse ``study_required`` (no threshold invent).
    """
    from coscientist.core.events.errors import StudyRequiredError

    if study is None:
        raise StudyRequiredError()
    k_ref = study.refutations_needed
    priority = study.refutation_priority
    if k is not None:
        conf_k = k
    else:
        conf_k = study.confirmations_needed
    supp_k = study.supported_needed
    by_target = filtered_claims_by_target(
        graph, study_id=study_id, results=results
    )
    if refs is not None:
        ref_list = list(refs)
    else:
        # One entry per kind-prefixed group (no Q/H merge).
        ref_list = list(by_target.keys())
    out: dict[str, dict[str, Any]] = {}
    for ref in ref_list:
        kind = resolve_verdict_ref_kind(graph, study_id, ref)
        _, logical = parse_target_ref(ref)
        claims = claims_for_ref(by_target, ref, prefer=kind)
        if not claims and refs is None:
            continue
        # Verdict from counting (valid) claims only; invalid experiments are
        # listed in excluded_claims and do not rewrite a normal verdict.
        counting = [c for c in claims if c.counts()]
        excluded = excluded_claims_report(claims)
        from coscientist.core.claims.verdict import hypothesis_verdict as _hyp_v

        v, by_lit, by_theory, _rule = _hyp_v(
            claims,
            any_experiment_done,
            refutations_needed=k_ref,
            refutation_priority=priority,
            supported_needed=supp_k,
            confirmations_needed=conf_k,
        )
        validity_excl = [e for e in excluded if e["reason"] == "validity_failed"]
        # Only when no valid claims remain: inconclusive + validity_failed.
        # Only-excluded (stub/partial/lit) → inconclusive, not untested.
        if not counting and (validity_excl or excluded):
            v = "inconclusive"
        provs: list[Provenance] = [
            c.provenance  # type: ignore[misc]
            for c in counting
            if c.provenance in VALID_PROVENANCE
        ]
        source = worst_provenance(*provs) if provs else "stub"
        snap: dict[str, Any] = {
            "ref": logical,
            "target_kind": kind,
            "verdict": v,
            "by_literature": by_lit,
            "by_theory": by_theory,
            "provenance": source,
            "refutations_needed": k_ref,
            "refutation_priority": priority,
            "k": conf_k,
            "supported_needed": supp_k,
        }
        if excluded:
            snap["excluded_claims"] = excluded
        if not counting and validity_excl:
            snap["reason"] = "validity_failed"
        note = priority_refuted_support_note(
            claims,
            any_experiment_done,
            conf_k,
            refutations_needed=k_ref,
            refutation_priority=priority,
            supported_needed=supp_k,
        )
        if note is not None:
            snap["note"] = note
        # Bind (refs set): key by InputSpec.ref logical id.
        # Full dump (refs None): kind-prefixed keys so Q/H cannot collide.
        if refs is None:
            out[f"{kind}:{logical}"] = snap
        else:
            out[logical] = snap
    return out


def exclusion_reason_for_claim(claim: RuntimeClaim) -> str | None:
    """Why a claim is out of the counting set (PR8b + PR8 roles)."""
    from coscientist.core.claims.verdict import exclusion_reason as _excl

    return _excl(claim)


def excluded_claims_report(
    claims: Sequence[RuntimeClaim],
) -> list[dict[str, Any]]:
    """List non-counting claims for the report payload."""
    out: list[dict[str, Any]] = []
    for c in claims:
        reason = exclusion_reason_for_claim(c)
        if reason is None:
            continue
        out.append(
            {
                "attempt_id": c.attempt_id,
                "node_id": c.node_id,
                "reason": reason,
            }
        )
    return out


def disposition_from_verdict(v: str) -> str | None:
    """Map RM verdict → question Disposition value; None if untested."""
    if v == "untested":
        return None
    return _VERDICT_TO_DISPOSITION.get(v)


def hypothesis_verdicts(
    graph: GraphStore,
    *,
    study_id: str,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int | None = None,
    any_experiment_done: bool = False,
) -> dict[str, dict[str, Any]]:
    """Verdicts for hypothesis targets (for report payload)."""
    by_target = filtered_claims_by_target(
        graph, study_id=study_id, results=results
    )
    hyp_refs = [
        key.split(":", 1)[-1]
        for key in by_target
        if key.startswith("hypothesis:")
    ]
    # Also include hypothesis nodes that may lack claims yet.
    for node in graph.nodes_by_kind("hypothesis"):
        logical = _logical_id_from_node(node.node_id, study_id, "hypothesis")
        if logical not in hyp_refs:
            hyp_refs.append(logical)
    # Pass kind-prefixed refs so bind-style lookup cannot pick up a question.
    qualified = [f"hypothesis:{r}" for r in hyp_refs]
    snaps = compute_verdict_snapshots(
        graph,
        study_id=study_id,
        study=study,
        results=results,
        k=k,
        any_experiment_done=any_experiment_done,
        refs=qualified,
    )
    out: dict[str, dict[str, Any]] = {}
    for ref in hyp_refs:
        snap = snaps.get(ref) or snaps.get(f"hypothesis:{ref}")
        if snap is None:
            continue
        hid = hypothesis_node_id(study_id, ref)
        if graph.get_node(hid) is not None or ref.startswith(f"{study_id}:hypothesis:"):
            out[ref] = snap
            continue
        for c in claims_for_ref(by_target, ref, prefer="hypothesis"):
            about = [e for e in graph.edges_from(c.node_id) if e.kind == "about"]
            if about and (n := graph.get_node(about[0].dst_id)) and n.kind == "hypothesis":
                out[ref] = snap
                break
    return out
