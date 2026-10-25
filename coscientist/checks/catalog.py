"""Requirement catalog loader (DESIGN §5). Unknown kind / role → CatalogError."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Mapping

from pydantic import Field, StrictInt, TypeAdapter, ValidationError

from coscientist.core.claims.graph import KindRegistry
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    RequirementTemplate,
    ReviewCheckMeasured,
    ReviewCheckPlain,
    RuleRef,
)

# Single path: measured discriminator (plain | measured); no ReviewCheck alias.
_ReviewCheckWire = Annotated[
    ReviewCheckPlain | ReviewCheckMeasured,
    Field(discriminator="measured"),
]
_REVIEW_CHECK_ADAPTER: TypeAdapter[_ReviewCheckWire] = TypeAdapter(_ReviewCheckWire)

# Re-export for callers/tests that type catalog generations.
CatalogVersionInt = StrictInt
TemplateVersionInt = StrictInt

KNOWN_CODE_KINDS = frozenset(
    {
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
        "quote_in_source",
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
        "rubric",
    }
)
# Sandbox-backed kinds: require pids_max / rlimit_nproc / reap_timeout_s /
# output_drain_join_s at load time and a frozen per-task limits row at evaluate
# time (``sandbox_limits``).
EXECUTABLE_CODE_KINDS: Final[frozenset[str]] = frozenset(
    {"exec_runs", "tests_pass", "metric_on_holdout", "reproducible_rerun"}
)
# Config constants shared by checks modules (single source; not bare literals).
LOG_SNIPPET_BYTES: Final[int] = 8000
# Single-token builtin argv markers (catalog fixtures / CodeCheck.argv).
BUILTIN_TESTS_PASS_ARGV: Final[tuple[str, ...]] = ("catalog:pytest",)
BUILTIN_METRIC_SCORER_ARGV: Final[tuple[str, ...]] = ("catalog:metric",)
# Guest-visible mount path of the catalog scorer / launcher inside the verifier
# jail (``build_verifier_bwrap`` mounts it here; argv tokens reference it).
VERIFIER_SCORER_GUEST_PATH: Final[str] = "/scorer"
# argv[0] token for catalog launcher/scorer runs and the isolated-python rewrite.
GUEST_PYTHON_ARGV0: Final[str] = "python3"
# argv[0] tokens that ``-I`` isolation is forced onto (exact match only).
ISOLATED_PYTHON_ARGV0_TOKENS: Final[frozenset[str]] = frozenset({"python", GUEST_PYTHON_ARGV0})
# Estimator kind stamped on numeric effect extras.
EFFECT_EST_KIND_POINT: Final[str] = "point"
# Sandbox host tuning (single source). ``SandboxLimits`` has no field defaults;
# ``limits_from_check`` passes these explicitly. Scientific budgets and the host
# post-exit budgets (reap / drain) are per-check catalog fields — no tuning here.
SANDBOX_TERM_GRACE_S: Final[float] = 0.5
SANDBOX_WATCH_POLL_INTERVAL_S: Final[float] = 0.02
SANDBOX_CGROUP_PROBE_INTERVAL_S: Final[float] = 0.01
SANDBOX_CGROUP_PROBE_MAX_ATTEMPTS: Final[int] = 50
SANDBOX_CPU_BUDGET_WALL_RATIO: Final[float] = 0.8
SANDBOX_CPU_BUDGET_WALL_MARGIN_S: Final[float] = 0.05
SANDBOX_CPU_BUDGET_FLOOR_S: Final[float] = 0.05
SANDBOX_IDLE_CPU_WALL_RATIO: Final[float] = 0.05
SANDBOX_RLIMIT_FUSE_EXTRA_RATIO: Final[float] = 0.10
SANDBOX_RLIMIT_FUSE_EXTRA_FLOOR_S: Final[float] = 1.0
SANDBOX_RLIMIT_FUSE_WALL_MARGIN_S: Final[float] = 0.05
SANDBOX_CGROUP_CPU_MAX: Final[str] = "100000 100000"
SANDBOX_REAP_POLL_INTERVAL_S: Final[float] = 0.05
# bwrap hard-isolation probe config.
PROBE_HOST_CANARY_PATHS: Final[tuple[str, ...]] = ("/etc/hostname", "/etc/passwd")
PROBE_NET_CANARY_HOST: Final[str] = "1.1.1.1"
PROBE_NET_CANARY_PORT: Final[int] = 80
PROBE_NET_CONNECT_TIMEOUT_S: Final[float] = 0.2
PROBE_SUBPROCESS_TIMEOUT_S: Final[int] = 10
PROBE_EXIT_HOST_VISIBLE: Final[int] = 11
PROBE_EXIT_NET_UP: Final[int] = 12
# ``catalog.defaults`` key injected for tasks with empty criteria (evaluate + freeze).
EXPERIMENT_DEFAULTS_KEY: Final[str] = "experiment"
# Template id whose frozen limits are always captured when present in a catalog.
PROPOSED_CHECK_TEMPLATE_ID: Final[str] = "proposed_check"
# Host-budget keys copied from a frozen catalog record onto CodeCheck.
FROZEN_LIMIT_KEYS: Final[tuple[str, ...]] = (
    "timeout_s",
    "memory_mb",
    "max_output_bytes",
    "pids_max",
    "rlimit_nproc",
    "reap_timeout_s",
    "output_drain_join_s",
)
KNOWN_REVIEW_ROLES = frozenset({"reviewer", "critic", "human"})
_PREDICTED_DIRECTIONS = frozenset({"positive", "negative", "two_sided"})


# Integer catalog/template generations (StrictInt — no str Enum / no coerce).
# v3: executable kinds require pids_max/rlimit_nproc/security toggles at load;
#     RequirementTemplate.exempt_from_freeze required bool.
# v4: ReviewCheck requires discriminator ``measured`` and ``max_calls`` (no default).

# PLAN-n critic markers are identified by the ``plan_expansion_`` prefix
# (DESIGN_M1 §4 / evaluate). No hardcoded id inventory — freeze skip uses the
# same prefix via ``sandbox_limits.is_plan_n_critic_template_id``. Replaced by
# Task.plan_role in #38.

CATALOG_VERSION: Final[int]
TEMPLATE_VERSION: Final[int] = 1

# HISTORY is the single source of truth for the current generation label.
# Prior blobs lack required fields and cannot be upgraded without inventing
# values, so they are refused with the reason below. A real migration must be
# an explicit loader step, not a wider allow-list.
CATALOG_VERSION_HISTORY: Final[Mapping[int, str]] = MappingProxyType(
    {
        1: "refused:no_security_toggles_or_exempt_from_freeze",
        2: "refused:no_pids_max_rlimit_nproc_for_executable_kinds",
        3: "refused:review_check_measured_discriminator_and_max_calls_required",
        4: "current",
    }
)


def _current_catalog_generation(history: Mapping[int, str]) -> int:
    found = [gen for gen, label in history.items() if label == "current"]
    if len(found) != 1:
        raise RuntimeError(f"CATALOG_VERSION_HISTORY must mark exactly one generation as current, got {found!r}")
    return found[0]


CATALOG_VERSION = _current_catalog_generation(CATALOG_VERSION_HISTORY)
SUPPORTED_CATALOG_VERSIONS: Final[frozenset[int]] = frozenset({CATALOG_VERSION})
# ``history=`` token for any string-typed (pre-integer) catalog_version.
LEGACY_STRING_CATALOG_VERSION_REFUSED: Final[str] = "refused:legacy_string_generation"
# ``history=`` token in the refuse message for a generation not in the chain.
UNKNOWN_CATALOG_GENERATION: Final[str] = "unknown_generation"
SUPPORTED_TEMPLATE_VERSIONS: Final[frozenset[int]] = frozenset({TEMPLATE_VERSION})


def _migrate_code_check(raw: dict[str, object]) -> dict[str, object]:
    """Refuse legacy ``holdout_output`` — never soft-pop / rewrite the field."""
    if "kind" not in raw:
        raise CatalogError("catalog_codecheck_v1:kind_required")
    if "holdout_output" in raw:
        raise CatalogError(
            "catalog_codecheck_v1:metric_on_holdout:legacy_holdout_output_removed:"
            "provide_holdout_and_holdout_digest_only"
        )
    return dict(raw)


def _parse_check(raw: dict[str, object]) -> CodeCheck | ReviewCheckPlain | ReviewCheckMeasured:
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in KNOWN_CODE_KINDS:
            raise CatalogError(f"catalog_codecheck_v1:unknown_kind:{kind}")
        migrated = _migrate_code_check(raw)
        # Kind-required fields are enforced here (catalog zone).
        if kind == "tests_pass" and "min_tests" not in migrated:
            raise CatalogError("catalog_codecheck_v1:tests_pass:min_tests_required")
        if kind == "reproducible_rerun" and "rerun_tol" not in migrated:
            raise CatalogError("catalog_codecheck_v1:reproducible_rerun:rerun_tol_required")
        if "allow_network" not in migrated:
            raise CatalogError(f"catalog_codecheck_v1:{kind}:allow_network_required")
        if "allow_cgroup_degraded" not in migrated:
            raise CatalogError(f"catalog_codecheck_v1:{kind}:allow_cgroup_degraded_required")
        if kind in EXECUTABLE_CODE_KINDS:
            if "pids_max" not in migrated:
                raise CatalogError(f"catalog_codecheck_v1:{kind}:pids_max_required")
            if "rlimit_nproc" not in migrated:
                raise CatalogError(f"catalog_codecheck_v1:{kind}:rlimit_nproc_required")
            for host_key in ("reap_timeout_s", "output_drain_join_s"):
                if host_key not in migrated:
                    raise CatalogError(f"catalog_codecheck_v1:{kind}:{host_key}_required")
        try:
            return CodeCheck.model_validate(migrated)
        except ValidationError as e:
            raise CatalogError(f"catalog_codecheck_v1:{kind}:{e}") from e
    if "reviewer_role" in raw:
        role = raw["reviewer_role"]
        if role not in KNOWN_REVIEW_ROLES:
            raise CatalogError(f"catalog_reviewcheck_v1:unknown_role:{role}")
        try:
            return _REVIEW_CHECK_ADAPTER.validate_python(raw)
        except ValidationError as e:
            raise CatalogError(f"catalog_reviewcheck_v1:{role}:{e}") from e
    raise CatalogError("catalog_check_v1:must_be_codecheck_or_reviewcheck")


def _require_exact_str(data: dict[str, object], key: str) -> str:
    """Non-empty str with no leading/trailing whitespace (no strip coercion)."""
    if key not in data:
        raise CatalogError(f"catalog_meta_v1:{key}_required")
    v = data[key]
    if not isinstance(v, str):
        raise CatalogError(f"catalog_meta_v1:{key}_required_str_got_{type(v).__name__}")
    if not v or v != v.strip():
        raise CatalogError(f"catalog_meta_v1:{key}_required")
    return v


def _require_strict_int(data: dict[str, object], key: str, *, ctx: str) -> int:
    """Require a real ``int`` (reject bool/str/float) — StrictInt semantics."""
    if key not in data:
        raise CatalogError(f"{ctx}:{key}_required")
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise CatalogError(f"{ctx}:{key}_must_be_strict_int_got_{type(v).__name__}")
    return v


def _require_template_bool(t: dict[str, object], key: str, tid: object) -> bool:
    if key not in t:
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_required")
    v = t[key]
    if not isinstance(v, bool):
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_must_be_bool_got_{type(v).__name__}")
    return v


def _require_template_exact_str(t: dict[str, object], key: str, tid: object) -> str:
    if key not in t:
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_required")
    v = t[key]
    if not isinstance(v, str):
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_must_be_str_got_{type(v).__name__}")
    if not v or v != v.strip():
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_required")
    return v


def _require_key_list(data: dict[str, object], key: str) -> list[object]:
    if key not in data:
        raise CatalogError(f"catalog_meta_v1:{key}_required")
    v = data[key]
    if not isinstance(v, list):
        raise CatalogError(f"catalog_meta_v1:{key}_must_be_list_got_{type(v).__name__}")
    return v


def _require_key_object(data: dict[str, object], key: str) -> dict[str, object]:
    if key not in data:
        raise CatalogError(f"catalog_meta_v1:{key}_required")
    v = data[key]
    if not isinstance(v, dict):
        raise CatalogError(f"catalog_meta_v1:{key}_must_be_object_got_{type(v).__name__}")
    return v


def _require_str_list(raw: object, *, ctx: str) -> list[str]:
    if not isinstance(raw, list):
        raise CatalogError(f"{ctx}_must_be_list_got_{type(raw).__name__}")
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str) or not x or x != x.strip():
            raise CatalogError(f"{ctx}_entry_must_be_exact_str_got_{type(x).__name__}")
        out.append(x)
    return out


def _optional_finite_float(v: object, *, tid: object, key: str) -> float | None:
    if v is None:
        return None
    # bool is a subclass of int — refuse before numeric accept.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_must_be_number_got_{type(v).__name__}")
    fv = float(v)
    if fv != fv or fv in (float("inf"), float("-inf")):  # NaN / inf
        raise CatalogError(f"catalog_template_v1:{tid}:{key}_must_be_finite")
    return fv


def _optional_template_float(t: dict[str, object], key: str, tid: object) -> float | None:
    if key not in t:
        return None
    return _optional_finite_float(t[key], tid=tid, key=key)


def load_catalog(
    data: dict[str, object] | Path | str,
    *,
    kind_registry: KindRegistry | None = None,
) -> Catalog:
    if isinstance(data, (str, Path)):
        try:
            text = Path(data).read_text(encoding="utf-8")
        except OSError as e:
            raise CatalogError(f"catalog_meta_v1:read_failed:{type(e).__name__}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CatalogError(f"catalog_meta_v1:json_decode:{e.__class__.__name__}") from e
    if not isinstance(data, dict):
        raise CatalogError(f"catalog_meta_v1:root_must_be_object_got_{type(data).__name__}")
    if kind_registry is None:
        raise CatalogError("catalog_meta_v1:kind_registry_required")

    if "catalog_version" in data and isinstance(data["catalog_version"], str):
        # Pre-v1 string generations (e.g. "0.2.0") carry no security toggles /
        # pid budgets and cannot be upgraded without inventing values.
        raise CatalogError(
            "catalog_meta_v1:catalog_version_must_be_strict_int_got_str:"
            f"{data['catalog_version']!r}:supported={sorted(SUPPORTED_CATALOG_VERSIONS)}:"
            f"history={LEGACY_STRING_CATALOG_VERSION_REFUSED}"
        )
    catalog_version = _require_strict_int(data, "catalog_version", ctx="catalog_meta_v1")
    if catalog_version not in SUPPORTED_CATALOG_VERSIONS:
        if catalog_version in CATALOG_VERSION_HISTORY:
            history = CATALOG_VERSION_HISTORY[catalog_version]
        else:
            history = UNKNOWN_CATALOG_GENERATION
        raise CatalogError(
            f"catalog_meta_v1:unsupported_catalog_version:{catalog_version}:"
            f"supported={sorted(SUPPORTED_CATALOG_VERSIONS)}:"
            f"history={history}"
        )
    edition = _require_exact_str(data, "edition")

    rules: list[RuleRef] = []
    for r in _require_key_list(data, "rules"):
        try:
            rules.append(RuleRef.model_validate(r))
        except ValidationError as e:
            raise CatalogError(f"catalog_ruleref_v1:{e}") from e

    templates: list[RequirementTemplate] = []
    for t in _require_key_list(data, "templates"):
        if not isinstance(t, dict):
            raise CatalogError(f"catalog_template_v1:entry_must_be_object_got_{type(t).__name__}")
        if "id" not in t:
            raise CatalogError("catalog_template_v1:id_required")
        tid = t["id"]
        if not isinstance(tid, str) or not tid or tid != tid.strip():
            raise CatalogError(f"catalog_template_v1:id_must_be_exact_str_got_{type(tid).__name__}")
        if "title" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:title_required")
        title = t["title"]
        if not isinstance(title, str) or not title or title != title.strip():
            raise CatalogError(f"catalog_template_v1:{tid}:title_must_be_exact_str_got_{type(title).__name__}")
        if "text_ru" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:text_ru_required")
        text_ru = t["text_ru"]
        if not isinstance(text_ru, str) or not text_ru or text_ru != text_ru.strip():
            raise CatalogError(f"catalog_template_v1:{tid}:text_ru_required")
        # Both method_sources and rule_ids are required (non-empty lists).
        if "method_sources" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:method_sources_required")
        if "rule_ids" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:rule_ids_required")
        method_sources = _require_str_list(
            t["method_sources"],
            ctx=f"catalog_template_v1:{tid}:method_sources",
        )
        rule_ids = _require_str_list(t["rule_ids"], ctx=f"catalog_template_v1:{tid}:rule_ids")
        if len(method_sources) == 0:
            raise CatalogError(f"catalog_template_v1:{tid}:method_sources_required")
        if len(rule_ids) == 0:
            raise CatalogError(f"catalog_template_v1:{tid}:rule_ids_required")
        tmpl_kwargs: dict[str, object] = {
            "method_sources": method_sources,
            "rule_ids": rule_ids,
        }
        if "check" not in t or not isinstance(t["check"], dict):
            raise CatalogError(f"catalog_template_v1:{tid}:check_required")
        check = _parse_check(t["check"])
        if "applies_to" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:applies_to_required")
        applies = _require_str_list(t["applies_to"], ctx=f"catalog_template_v1:{tid}:applies_to")
        if len(applies) == 0:
            raise CatalogError(f"catalog_template_v1:{tid}:applies_to_required_nonempty")
        for k in applies:
            try:
                kind_registry.require_node_kind(k)
            except KeyError as e:
                raise CatalogError(f"catalog_template_v1:{tid}:unknown_node_kind:{e}") from e
        if "role" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:role_required")
        role = t["role"]
        if role not in ("prediction", "validity"):
            raise CatalogError(f"catalog_template_v1:{tid}:role_required_got_{role!r}")
        uses_sesoi = _require_template_bool(t, "uses_sesoi", tid)
        predicts_null = _require_template_bool(t, "predicts_null", tid)
        required = _require_template_bool(t, "required", tid)
        exempt = _require_template_bool(t, "exempt_from_freeze", tid)
        version = _require_strict_int(t, "version", ctx=f"catalog_template_v1:{tid}")
        if version not in SUPPORTED_TEMPLATE_VERSIONS:
            raise CatalogError(f"catalog_template_v1:{tid}:unsupported_version:{version}")
        if "predicted_direction" not in t:
            raise CatalogError(f"catalog_template_v1:{tid}:predicted_direction_required")
        direction = t["predicted_direction"]
        if direction not in _PREDICTED_DIRECTIONS:
            raise CatalogError(f"catalog_template_v1:{tid}:predicted_direction_required_got_{direction!r}")
        # Absent key = the template carries no such bound (None); present values
        # are validated, never clamped — out-of-range sesoi refuses below.
        sesoi_f = _optional_template_float(t, "sesoi", tid)
        sesoi_min_f = _optional_template_float(t, "sesoi_min", tid)
        sesoi_max_f = _optional_template_float(t, "sesoi_max", tid)
        if "min_power" in t:
            raise CatalogError(f"catalog_template_v1:{tid}:min_power_unsupported_m1")
        if uses_sesoi:
            if sesoi_f is None:
                raise CatalogError(f"catalog_template_v1:{tid}:uses_sesoi_requires_sesoi")
            if sesoi_min_f is not None and sesoi_f < sesoi_min_f:
                raise CatalogError(f"catalog_template_v1:{tid}:sesoi_below_min")
            if sesoi_max_f is not None and sesoi_f > sesoi_max_f:
                raise CatalogError(f"catalog_template_v1:{tid}:sesoi_above_max")
        elif sesoi_f is not None:
            raise CatalogError(f"catalog_template_v1:{tid}:sesoi_without_uses_sesoi")
        if predicts_null and not uses_sesoi:
            raise CatalogError(f"catalog_template_v1:{tid}:predicts_null_requires_uses_sesoi")
        templates.append(
            RequirementTemplate(
                id=tid,
                title=title,
                text_ru=text_ru,
                applies_to=applies,
                check=check,
                role=role,
                required=required,
                version=version,
                uses_sesoi=uses_sesoi,
                sesoi=sesoi_f,
                sesoi_min=sesoi_min_f,
                sesoi_max=sesoi_max_f,
                predicts_null=predicts_null,
                predicted_direction=direction,
                exempt_from_freeze=exempt,
                **tmpl_kwargs,
            )
        )

    defaults_raw = _require_key_object(data, "defaults")
    defaults: dict[str, list[str]] = {}
    for k, v in defaults_raw.items():
        if not isinstance(k, str) or not k or k != k.strip():
            raise CatalogError(f"catalog_meta_v1:defaults_key_must_be_exact_str_got_{type(k).__name__}")
        defaults[k] = _require_str_list(v, ctx=f"catalog_meta_v1:defaults[{k}]")
    for k in defaults:
        try:
            kind_registry.require_node_kind(k)
        except KeyError as e:
            raise CatalogError(f"catalog_meta_v1:defaults:unknown_node_kind:{e}") from e

    covered = {rid for t in templates for rid in t.rule_ids}
    for rule in rules:
        if rule.rule_id not in covered:
            raise CatalogError(f"catalog_meta_v1:RuleRef_{rule.rule_id}_uncovered")

    tmpl_ids = {t.id for t in templates}
    for kind, ids in defaults.items():
        for tid in ids:
            if tid not in tmpl_ids:
                raise CatalogError(f"catalog_meta_v1:defaults[{kind}]_unknown_template_{tid}")

    code_outputs = _require_str_list(
        _require_key_list(data, "code_output_names"),
        ctx="catalog_meta_v1:code_output_names",
    )
    sources_raw = _require_key_object(data, "sources_sha256")
    sources: dict[str, str] = {}
    for k, v in sources_raw.items():
        if not isinstance(k, str) or not k or k != k.strip():
            raise CatalogError(f"catalog_meta_v1:sources_sha256_key_must_be_exact_str_got_{type(k).__name__}")
        if not isinstance(v, str) or not v or v != v.strip():
            raise CatalogError(f"catalog_meta_v1:sources_sha256_value_must_be_exact_str_got_{type(v).__name__}")
        sources[k] = v
    return Catalog(
        catalog_version=catalog_version,
        edition=edition,
        sources_sha256=sources,
        rules=rules,
        templates=templates,
        defaults=defaults,
        code_output_names=code_outputs,
    )
