"""Fail-closed sandbox limit freeze / apply (#41 sandbox zone)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import coscientist.checks.sandbox as sb
from coscientist.checks.sandbox_limits import (
    FROZEN_LIMIT_KEYS,
    _catalog_limits_for_criteria,
    apply_frozen_limits,
    freeze_task_sandbox_limits,
    is_plan_n_critic_template_id,
)
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    FrozenCheckLimits,
    RequirementRef,
    RequirementTemplate,
    ReviewCheck,
    TaskSpec,
)
from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA
from tests.checks.sandbox_test_limits import (
    TEST_CHECK_DRAIN_JOIN_S,
    TEST_CHECK_REAP_TIMEOUT_S,
    TEST_DRAIN_JOIN_S,
    TEST_PIDS_MAX,
    TEST_REAP_TIMEOUT_S,
    TEST_RLIMIT_NPROC,
    sandbox_limits,
)

PLAN_N_FREEZE_SKIP_IDS = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)


def _row(**over: object) -> FrozenCheckLimits:
    fields: dict[str, object] = {
        "timeout_s": 2.0,
        "memory_mb": 64,
        "max_output_bytes": 1024,
        "pids_max": 8,
        "rlimit_nproc": 8,
        "reap_timeout_s": 1.0,
        "output_drain_join_s": 1.0,
        "allow_network": False,
        "allow_cgroup_degraded": False,
    }
    fields.update(over)
    return FrozenCheckLimits(**fields)  # type: ignore[arg-type]


def test_apply_frozen_limits_rejects_coercible_host_budget_strings():
    check = CodeCheck(
        kind="exec_runs",
        timeout_s=1.0,
        memory_mb=64,
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    # Typed freeze row refuses str/bool budgets at construction — never coerced.
    for bad in ("5", True, 0, -1.0, float("nan")):
        with pytest.raises(ValidationError, match="host budget"):
            _row(reap_timeout_s=bad)
        with pytest.raises(ValidationError, match="host budget"):
            _row(output_drain_join_s=bad)
    with pytest.raises(ValidationError):
        _row(memory_mb="64")
    with pytest.raises(ValidationError):
        _row(pids_max=True)
    with pytest.raises(ValidationError):
        _row(timeout_s="1")
    # Raw dict rows on TaskSpec go through the same typed validation.
    with pytest.raises(ValidationError, match="host budget"):
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            sandbox_check_limits={
                "exec_busy": {**_row().model_dump(), "reap_timeout_s": "5"}
            },
        )
    assert check.kind == "exec_runs"


def test_apply_frozen_limits_refuses_without_frozen_record():
    """Agent cannot set allow_* via raw CodeCheck when freeze is missing."""
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        timeout_s=1.0,
        memory_mb=64,
        allow_network=True,
        allow_cgroup_degraded=True,
    )
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen:missing_spec"):
        apply_frozen_limits(check, None, template_id="exec_busy")
    bare = TaskSpec(task_key="E1", title="t", goal="g", route="experiment")
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen:missing_spec"):
        apply_frozen_limits(check, bare, template_id=None)
    spec = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        sandbox_check_limits=None,
    )
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen:exec_busy"):
        apply_frozen_limits(check, spec, template_id="exec_busy")
    # Frozen record without security toggles cannot even be built (typed row).
    with pytest.raises(ValidationError) as ei:
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            sandbox_check_limits={"exec_busy": {"timeout_s": 2.0}},
        )
    missing = {e["loc"][-1] for e in ei.value.errors()}
    assert {"allow_network", "allow_cgroup_degraded"} <= missing
    # Catalog freeze wins over agent-supplied allow_* on the check.
    frozen = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        sandbox_check_limits={"exec_busy": _row()},
    )
    applied = apply_frozen_limits(check, frozen, template_id="exec_busy")
    assert applied.allow_network is False
    assert applied.allow_cgroup_degraded is False
    assert applied.timeout_s == 2.0
    # Sandbox kinds always require a freeze record (no fail-open passthrough).
    other = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=1.0,
        memory_mb=64,
        max_output_bytes=1024,
        pids_max=8,
        rlimit_nproc=8,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen"):
        apply_frozen_limits(other, None, template_id="vm")
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen:vm"):
        apply_frozen_limits(other, bare, template_id="vm")
    # Non-sandbox kinds never run in the jail: returned unchanged (#39 core freeze).
    plain = CodeCheck(
        kind="value_matches",
        output="main",
        target=1,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    assert apply_frozen_limits(plain, None, template_id="vm") is plain
    sneaky = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=1.0,
        memory_mb=64,
        max_output_bytes=1024,
        pids_max=8,
        rlimit_nproc=8,
        allow_cgroup_degraded=False,
        allow_network=True,
    )
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen"):
        apply_frozen_limits(sneaky, None, template_id="vm")
    with pytest.raises(CatalogError, match="sandbox_limits_unfrozen:vm"):
        apply_frozen_limits(sneaky, bare, template_id="vm")


def test_apply_frozen_limits_rejects_string_allow_network():
    """``bool('false')`` is True — the typed row requires real bools."""
    for bad in ("false", "true", 0, 1, "0", "1", None):
        with pytest.raises(ValidationError, match="security toggle must be bool"):
            _row(allow_network=bad)
        with pytest.raises(ValidationError, match="security toggle must be bool"):
            _row(allow_cgroup_degraded=bad)
        with pytest.raises(ValidationError, match="security toggle must be bool"):
            TaskSpec(
                task_key="E1",
                title="t",
                goal="g",
                route="experiment",
                sandbox_check_limits={
                    "exec_busy": {**_row().model_dump(), "allow_network": bad}
                },
            )


def test_catalog_limits_unknown_template_raises():
    """Missing catalog template raises; agent ``exempt_from_freeze`` is forbidden."""
    cat = Catalog(catalog_version=3, edition="test", templates=[])
    with pytest.raises(CatalogError, match="sandbox_limits_unknown_template:missing"):
        _catalog_limits_for_criteria(
            cat, [RequirementRef(template_id="missing")]
        )
    # Checks-owned PLAN-n skip ids: freeze without a catalog row (until #38 plan_role).
    assert PLAN_N_FREEZE_SKIP_IDS
    skip_refs = [RequirementRef(template_id=tid) for tid in sorted(PLAN_N_FREEZE_SKIP_IDS)]
    assert _catalog_limits_for_criteria(cat, skip_refs) == {}
    frozen = freeze_task_sandbox_limits(
        TaskSpec(
            task_key="PLAN-1",
            title="replan",
            goal="g",
            route="planner",
            criteria=skip_refs,
        ),
        cat,
    )
    assert frozen.sandbox_check_limits == {}
    # RequirementRef forbids exempt_from_freeze (extra=forbid) — ValidationError.
    with pytest.raises(ValidationError, match="exempt_from_freeze"):
        RequirementRef(template_id="missing", exempt_from_freeze=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="exempt_from_freeze"):
        RequirementRef(template_id="x", exempt_from_freeze="true")  # type: ignore[call-arg]
    # Catalog-authored exemption skips freeze.
    cat_exempt = Catalog(
        catalog_version=3,
        edition="test",
        templates=[
            RequirementTemplate(
                id="custom_exempt",
                title="marker",
                text_ru="marker",
                method_sources=["test"],
                rule_ids=["R23"],
                applies_to=["experiment"],
                check=ReviewCheck(reviewer_role="critic", question="q", metric=None, op=None, target=None),
                role="validity",
                version=1,
                exempt_from_freeze=True,
            )
        ],
    )
    assert (
        _catalog_limits_for_criteria(
            cat_exempt, [RequirementRef(template_id="custom_exempt")]
        )
        == {}
    )
    # Template still carries catalog-authored exempt_from_freeze.
    assert cat_exempt.templates[0].exempt_from_freeze is True
    # Non-CodeCheck without exempt → CatalogError (no silent continue).
    cat_review = Catalog(
        catalog_version=3,
        edition="test",
        templates=[
            RequirementTemplate(
                id="review_only",
                title="marker",
                text_ru="marker",
                method_sources=["test"],
                rule_ids=["R23"],
                applies_to=["experiment"],
                check=ReviewCheck(reviewer_role="critic", question="q", metric=None, op=None, target=None),
                role="validity",
                version=1,
            exempt_from_freeze=False,
            )
        ],
    )
    with pytest.raises(CatalogError, match="sandbox_limits_non_codecheck:review_only"):
        _catalog_limits_for_criteria(
            cat_review, [RequirementRef(template_id="review_only")]
        )


def test_frozen_limit_keys_exported():
    assert "timeout_s" in FROZEN_LIMIT_KEYS
    assert "allow_network" not in FROZEN_LIMIT_KEYS  # security toggles separate


def test_plan_n_freeze_skip_ids_match_expand_critic():
    """Freeze skip set must match core PLAN-n critic criterion template ids."""
    from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA

    critic_ids = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)
    assert PLAN_N_FREEZE_SKIP_IDS == critic_ids


def test_catalog_none_refuses():
    with pytest.raises(CatalogError, match="sandbox_limits_catalog_required"):
        _catalog_limits_for_criteria(None, [RequirementRef(template_id="x")])
    with pytest.raises(CatalogError, match="sandbox_limits_catalog_required"):
        freeze_task_sandbox_limits(
            TaskSpec(
                task_key="E1",
                title="t",
                goal="g",
                route="experiment",
                criteria=[RequirementRef(template_id="x")],
            ),
            None,
        )


_LIMITS_BASE: dict[str, object] = dict(
    timeout_s=1.0,
    memory_mb=64,
    max_output_bytes=1000,
    pids_max=TEST_PIDS_MAX,
    rlimit_nproc=TEST_RLIMIT_NPROC,
    allow_network=False,
    allow_cgroup_degraded=False,
)


def test_limits_from_check_passes_host_budgets_and_refuses_none():
    from coscientist.core.events.errors import CatalogError

    lim = sb.limits_from_check(
        **_LIMITS_BASE,  # type: ignore[arg-type]
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )
    assert lim.reap_timeout_s == TEST_REAP_TIMEOUT_S
    assert lim.output_drain_join_s == TEST_DRAIN_JOIN_S
    with pytest.raises(CatalogError, match="sandbox_limits_bad_type"):
        sb.limits_from_check(
            **_LIMITS_BASE,  # type: ignore[arg-type]
            reap_timeout_s=None,
            output_drain_join_s=None,
        )


def test_limits_from_check_host_budget_args_are_required_keywords():
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(TypeError, match="reap_timeout_s"):
        sb.limits_from_check(**_LIMITS_BASE, output_drain_join_s=None)  # type: ignore[arg-type,call-arg]
    with pytest.raises(TypeError, match="output_drain_join_s"):
        sb.limits_from_check(**_LIMITS_BASE, reap_timeout_s=None)  # type: ignore[arg-type,call-arg]
    with pytest.raises((TypeError, CatalogError, ValueError)):
        sb.limits_from_check(
            **_LIMITS_BASE,  # type: ignore[arg-type]
            reap_timeout_s=True,  # type: ignore[arg-type]
            output_drain_join_s=1.0,
        )


def test_limits_from_check_requires_strict_security_bools():
    from coscientist.core.events.errors import CatalogError

    base = {
        k: v
        for k, v in _LIMITS_BASE.items()
        if k not in ("allow_network", "allow_cgroup_degraded")
    }
    host = dict(reap_timeout_s=TEST_REAP_TIMEOUT_S, output_drain_join_s=TEST_DRAIN_JOIN_S)
    with pytest.raises(CatalogError, match="sandbox_limits_bad_toggle:allow_network"):
        sb.limits_from_check(
            **base, **host, allow_network="false", allow_cgroup_degraded=False  # type: ignore[arg-type]
        )
    with pytest.raises(
        CatalogError, match="sandbox_limits_bad_toggle:allow_cgroup_degraded"
    ):
        sb.limits_from_check(
            **base, **host, allow_network=False, allow_cgroup_degraded=0  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="allow_network"):
        sb.limits_from_check(**base, **host)  # type: ignore[arg-type,call-arg]


def test_sandbox_limits_rejects_cpu_budget_below_floor():
    """Derived CPU budget < floor raises in __post_init__ (no silent clamp)."""
    with pytest.raises(ValueError, match="cpu budget"):
        sandbox_limits(
            timeout_s=1.0,
            memory_mb=64,
            max_output_bytes=1024,
            cpu_budget_floor_s=0.5,
            cpu_budget_wall_ratio=0.2,
            cpu_budget_wall_margin_s=0.05,
        )


def test_reap_timeout_error_is_not_oserror():
    assert not issubclass(sb.ReapTimeoutError, OSError)
    assert not issubclass(sb.ReapTimeoutError, TimeoutError)


def test_limits_from_check_rejects_missing_scientific_budgets():
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="sandbox_limits_bad_type"):
        sb.limits_from_check(
            timeout_s=None,
            memory_mb=None,
            max_output_bytes=None,
            pids_max=None,
            rlimit_nproc=None,
            allow_network=False,
            allow_cgroup_degraded=False,
            reap_timeout_s=None,
            output_drain_join_s=None,
        )


def test_freeze_empty_limits_is_empty_dict_not_none():
    """Empty freeze map stays ``{}`` — no ``or None`` collapse."""
    cat = Catalog(catalog_version=3, edition="test", templates=[])
    skip_refs = [RequirementRef(template_id=tid) for tid in sorted(PLAN_N_FREEZE_SKIP_IDS)]
    frozen = freeze_task_sandbox_limits(
        TaskSpec(
            task_key="PLAN-1",
            title="replan",
            goal="g",
            route="planner",
            criteria=skip_refs,
        ),
        cat,
    )
    assert frozen.sandbox_check_limits == {}
    assert frozen.sandbox_check_limits is not None


def test_no_sandbox_limits_from_catalog_alias():
    import coscientist.checks.sandbox_limits as mod

    assert not hasattr(mod, "sandbox_limits_from_catalog")


def test_exempt_checked_before_duplicate_skip():
    """Exempt templates resolve (and skip) even when the tid already appears."""
    cat = Catalog(
        catalog_version=3,
        edition="test",
        templates=[
            RequirementTemplate(
                id="custom_exempt",
                title="marker",
                text_ru="marker",
                method_sources=["test"],
                rule_ids=["R23"],
                applies_to=["experiment"],
                check=ReviewCheck(reviewer_role="critic", question="q", metric=None, op=None, target=None),
                role="validity",
                version=1,
                exempt_from_freeze=True,
            )
        ],
    )
    refs = [
        RequirementRef(template_id="custom_exempt"),
        RequirementRef(template_id="custom_exempt"),
    ]
    assert _catalog_limits_for_criteria(cat, refs) == {}


def test_apply_frozen_limits_rejects_non_str_template_id():
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=1.0,
        memory_mb=64,
        max_output_bytes=1024,
        pids_max=16,
        rlimit_nproc=16,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    spec = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="exec_ok")],
        sandbox_check_limits={"exec_ok": _row(timeout_s=1.0)},
    )
    with pytest.raises(CatalogError, match="sandbox_limits_bad_template_id"):
        apply_frozen_limits(check, spec, template_id=123)  # type: ignore[arg-type]


def test_plan_n_skip_is_exact_set_from_critic_criteria():
    """Membership in PLAN_N_CRITIC_CRITERIA — not a startswith heuristic."""
    from coscientist.checks.sandbox_limits import PLAN_N_CRITIC_TEMPLATE_IDS

    assert PLAN_N_CRITIC_TEMPLATE_IDS == PLAN_N_FREEZE_SKIP_IDS
    assert all(is_plan_n_critic_template_id(tid) for tid in PLAN_N_FREEZE_SKIP_IDS)
    assert not is_plan_n_critic_template_id("artifact_present")
    assert is_plan_n_critic_template_id("plan_expansion_schema")
    # Prefix lookalike must NOT match — set membership only.
    assert not is_plan_n_critic_template_id("plan_expansion_not_in_criteria")


def test_limits_from_check_refuses_int_for_float_budgets():
    """Float host budgets must be float — int is refused (no coercion)."""
    with pytest.raises(CatalogError, match="sandbox_limits_bad_type:timeout_s:int"):
        sb.limits_from_check(
            timeout_s=1,  # type: ignore[arg-type]
            memory_mb=64,
            max_output_bytes=1000,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=False,
            reap_timeout_s=TEST_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_DRAIN_JOIN_S,
        )
