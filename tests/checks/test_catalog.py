"""Catalog loader validation (DESIGN §5.2)."""

from __future__ import annotations

import pytest

from coscientist.checks.catalog import (
    SUPPORTED_CATALOG_VERSIONS,
    SUPPORTED_TEMPLATE_VERSIONS,
    load_catalog,
)
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.errors import CatalogError
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _lc(data, *, kind_registry=None, **kw):
    """load_catalog with required kind_registry (fail-closed)."""
    reg = default_execution_registry() if kind_registry is None else kind_registry
    return load_catalog(data, kind_registry=reg, **kw)



def _full_template(**overrides: object) -> dict:
    """Complete catalog_version 3 template row (no soft omissions)."""
    row: dict = {
        "id": "x",
        "title": "x",
        "text_ru": "текст",
        "method_sources": ["m"],
        "rule_ids": ["R23"],
        "applies_to": ["experiment"],
        "check": {"kind": "artifact_exists", "output": "main", "allow_network": False, "allow_cgroup_degraded": False},
        "role": "validity",
        "required": True,
        "version": 1,
        "uses_sesoi": False,
        "predicts_null": False,
        "predicted_direction": "two_sided",
        "exempt_from_freeze": False,
    }
    row.update(overrides)
    return row


def _full_catalog(**overrides: object) -> dict:
    """Complete catalog root for load_catalog tests."""
    data: dict = {
        "catalog_version": 3,
        "edition": "test",
        "sources_sha256": {},
        "rules": [
            {
                "rule_id": "R23",
                "source": "reviewed-rules",
                "excerpt": "x",
                "digest": "abc",
            }
        ],
        "templates": [_full_template()],
        "defaults": {},
        "code_output_names": [],
    }
    data.update(overrides)
    return data


def test_minimal_catalog_loads():
    cat = minimal_test_catalog()
    assert cat.catalog_version == 3
    assert cat.catalog_version in SUPPORTED_CATALOG_VERSIONS
    assert all(t.text_ru for t in cat.templates)
    assert all(int(t.version) in SUPPORTED_TEMPLATE_VERSIONS for t in cat.templates)


def test_unsupported_catalog_version_refuses():
    """Allow-list refuse — StrictInt generations only (legacy string 0.2.0 refused)."""
    with pytest.raises(
        CatalogError, match=r"catalog_meta_v1:unsupported_catalog_version:1:supported="
    ):
        _lc(_full_catalog(catalog_version=1))
    with pytest.raises(
        CatalogError, match=r"catalog_meta_v1:unsupported_catalog_version:99:supported="
    ):
        _lc(_full_catalog(catalog_version=99))
    with pytest.raises(
        CatalogError, match=r"catalog_meta_v1:catalog_version_must_be_strict_int_got_str"
    ):
        _lc(_full_catalog(catalog_version="0.2.0"))  # type: ignore[arg-type]


def test_unsupported_template_version_refuses():
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:unsupported_version:2"
    ):
        _lc(_full_catalog(templates=[_full_template(version=2)]))
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:version_must_be_strict_int_got_str"
    ):
        _lc(_full_catalog(templates=[_full_template(version="2")]))


def test_unknown_kind_errors():
    with pytest.raises(CatalogError, match="catalog_codecheck_v1:unknown_kind"):
        _lc(
            _full_catalog(
                templates=[_full_template(check={"kind": "not_a_real_kind"})],
                defaults={"experiment": ["x"]},
            )
        )


def test_unknown_node_kind_with_registry():
    reg = default_execution_registry()
    with pytest.raises(CatalogError, match="catalog_template_v1:x:unknown_node_kind"):
        _lc(
            _full_catalog(
                templates=[_full_template(applies_to=["not_in_schema"])],
                defaults={"not_in_schema": ["x"]},
            ),
            kind_registry=reg,
        )


def test_missing_text_ru():
    t = _full_template()
    del t["text_ru"]
    with pytest.raises(CatalogError, match="catalog_template_v1:x:text_ru_required"):
        _lc(_full_catalog(templates=[t]))


def test_text_ru_must_be_str_not_truthy_other():
    for bad in (1, True, ["текст"], {"ru": "текст"}):
        with pytest.raises(CatalogError, match="catalog_template_v1:x:text_ru_required"):
            _lc(_full_catalog(templates=[_full_template(text_ru=bad)]))


def test_no_strip_coercion_on_version_fields():
    with pytest.raises(
        CatalogError, match="catalog_meta_v1:catalog_version_must_be_strict_int_got_str"
    ):
        _lc(_full_catalog(catalog_version=" 3 "))  # type: ignore[arg-type]
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:version_must_be_strict_int_got_str"
    ):
        _lc(_full_catalog(templates=[_full_template(version=" 1 ")]))


def test_sesoi_rejects_bool_and_str_coercion():
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:sesoi_must_be_number"
    ):
        _lc(
            _full_catalog(
                templates=[
                    _full_template(
                        uses_sesoi=True, sesoi="0.5", predicted_direction="positive"
                    )
                ]
            )
        )
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:sesoi_must_be_number"
    ):
        _lc(
            _full_catalog(
                templates=[
                    _full_template(
                        uses_sesoi=True, sesoi=True, predicted_direction="positive"
                    )
                ]
            )
        )


def test_defaults_rejects_string_list_coercion():
    with pytest.raises(
        CatalogError, match="catalog_meta_v1:defaults\\[experiment\\]_must_be_list"
    ):
        _lc(_full_catalog(defaults={"experiment": "artifact_present"}))


def test_ruleref_validation_error_is_versioned():
    with pytest.raises(CatalogError, match="catalog_ruleref_v1:"):
        _lc(
            _full_catalog(
                rules=[{"rule_id": "R23"}],  # missing required RuleRef fields
            )
        )


def test_load_catalog_requires_version_and_edition():
    with pytest.raises(CatalogError, match="catalog_meta_v1:catalog_version_required"):
        _lc({"edition": "test", "templates": [], "defaults": {}})
    with pytest.raises(CatalogError, match="catalog_meta_v1:edition_required"):
        _lc({"catalog_version": 3, "templates": [], "defaults": {}})


def test_load_catalog_requires_top_level_collections():
    base = _full_catalog(templates=[], rules=[], defaults={})
    for key in (
        "rules",
        "templates",
        "defaults",
        "code_output_names",
        "sources_sha256",
    ):
        bad = dict(base)
        del bad[key]
        with pytest.raises(CatalogError, match=f"catalog_meta_v1:{key}_required"):
            _lc(bad)


def test_load_catalog_requires_strict_template_bools_and_version():
    base_t = {
        "id": "x",
        "title": "x",
        "text_ru": "текст",
        "method_sources": ["m"],
        "rule_ids": ["R23"],
        "applies_to": ["experiment"],
        "check": {"kind": "artifact_exists", "output": "main", "allow_network": False, "allow_cgroup_degraded": False},
        "role": "validity",
    }
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:uses_sesoi_required"
    ):
        _lc(_full_catalog(templates=[base_t], defaults={}))


def test_both_method_sources_and_rule_ids_required():
    """Both lists required non-empty — not or-semantics."""
    missing_rules = _full_template()
    del missing_rules["rule_ids"]
    with pytest.raises(CatalogError, match="catalog_template_v1:x:rule_ids_required"):
        _lc(_full_catalog(templates=[missing_rules]))
    missing_methods = _full_template()
    del missing_methods["method_sources"]
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:method_sources_required"
    ):
        _lc(_full_catalog(templates=[missing_methods]))
    with pytest.raises(CatalogError, match="catalog_template_v1:x:rule_ids_required"):
        _lc(_full_catalog(templates=[_full_template(rule_ids=[])]))
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:method_sources_required"
    ):
        _lc(_full_catalog(templates=[_full_template(method_sources=[])]))


def test_empty_applies_to_refuses():
    with pytest.raises(
        CatalogError, match="catalog_template_v1:x:applies_to_required_nonempty"
    ):
        _lc(_full_catalog(templates=[_full_template(applies_to=[])]))


def test_json_decode_is_catalog_error(tmp_path):
    bad = tmp_path / "bad_catalog.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CatalogError, match="catalog_meta_v1:json_decode"):
        _lc(bad)


def test_executable_check_requires_pids_and_security_toggles():
    base_check = {
        "kind": "exec_runs",
        "argv": ["/bin/true"],
        "timeout_s": 5.0,
        "memory_mb": 64,
        "max_output_bytes": 1000,
    }
    with pytest.raises(
        CatalogError, match="catalog_codecheck_v1:exec_runs:allow_network_required"
    ):
        _lc(_full_catalog(templates=[_full_template(check=dict(base_check))]))
    with_net = {**base_check, "allow_network": False}
    with pytest.raises(
        CatalogError,
        match="catalog_codecheck_v1:exec_runs:allow_cgroup_degraded_required",
    ):
        _lc(_full_catalog(templates=[_full_template(check=dict(with_net))]))
    with_toggles = {
        **with_net,
        "allow_cgroup_degraded": False,
    }
    with pytest.raises(
        CatalogError, match="catalog_codecheck_v1:exec_runs:pids_max_required"
    ):
        _lc(_full_catalog(templates=[_full_template(check=dict(with_toggles))]))
    with_pids = {**with_toggles, "pids_max": 16, "rlimit_nproc": 16}
    with pytest.raises(
        CatalogError, match="catalog_codecheck_v1:exec_runs:reap_timeout_s_required"
    ):
        _lc(_full_catalog(templates=[_full_template(check=dict(with_pids))]))
    with pytest.raises(
        CatalogError,
        match="catalog_codecheck_v1:exec_runs:output_drain_join_s_required",
    ):
        _lc(
            _full_catalog(
                templates=[
                    _full_template(check={**with_pids, "reap_timeout_s": 1.0})
                ]
            )
        )
    ok = {**with_pids, "reap_timeout_s": 1.0, "output_drain_join_s": 1.0}
    cat = _lc(_full_catalog(templates=[_full_template(check=ok)]))
    assert cat.templates[0].check.pids_max == 16  # type: ignore[union-attr]
    assert cat.templates[0].check.reap_timeout_s == 1.0  # type: ignore[union-attr]


def test_load_catalog_old_v1_missing_required_meta_refuses_no_upcast():
    old = {
        "catalog_version": "0.2.0",  # legacy string generation
        "edition": "legacy",
        "sources_sha256": {},
        "rules": [],
        "templates": [
            {
                "id": "legacy_row",
                "title": "legacy",
                "text_ru": "старый шаблон без новых полей",
                "method_sources": ["m"],
                "applies_to": ["experiment"],
                "check": {
                    "kind": "artifact_exists",
                    "output": "main",
                    "allow_network": False,
                    "allow_cgroup_degraded": False,
                },
                "role": "validity",
            }
        ],
        "defaults": {},
        "code_output_names": [],
    }
    with pytest.raises(
        CatalogError,
        match=r"catalog_meta_v1:catalog_version_must_be_strict_int_got_str",
    ):
        _lc(old)


def test_load_catalog_old_tests_pass_without_min_tests_is_catalog_error():
    with pytest.raises(
        CatalogError, match="catalog_codecheck_v1:tests_pass:min_tests_required"
    ):
        _lc(
            _full_catalog(
                edition="legacy",
                templates=[
                    _full_template(
                        id="tp",
                        title="tp",
                        text_ru="тесты",
                        check={
                            "kind": "tests_pass",
                            "argv": ["catalog:pytest"],
                            "tests": {"t.py": "def test_x():\n    assert True\n"},
                            "timeout_s": 5.0,
                            "memory_mb": 64,
                            "max_output_bytes": 1000,
                            "pids_max": 16,
                            "rlimit_nproc": 16,
                            "reap_timeout_s": 1.0,
                            "output_drain_join_s": 1.0,
                        },
                    )
                ],
            )
        )


def test_text_ru_and_check_same_source():
    cat = minimal_test_catalog()
    t = next(x for x in cat.templates if x.id == "value_match")
    assert "Значение" in t.text_ru
    assert t.check.kind == "value_matches"  # type: ignore[union-attr]
    from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA
    PLAN_N_FREEZE_SKIP_IDS = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)

    markers = [t for t in cat.templates if t.id in PLAN_N_FREEZE_SKIP_IDS]
    assert {t.id for t in markers} == set(PLAN_N_FREEZE_SKIP_IDS)
    assert all(t.exempt_from_freeze is True for t in markers)


def test_kind_registry_none_refuses():
    with pytest.raises(CatalogError, match="catalog_meta_v1:kind_registry_required"):
        load_catalog(_full_catalog(), kind_registry=None)


def test_holdout_output_never_soft_dropped():
    """Legacy holdout_output is refused even when holdout is also present."""
    with pytest.raises(
        CatalogError,
        match="catalog_codecheck_v1:metric_on_holdout:legacy_holdout_output_removed",
    ):
        _lc(
            _full_catalog(
                templates=[
                    _full_template(
                        id="ho",
                        check={
                            "kind": "metric_on_holdout",
                            "output": "main",
                            "metric": "acc",
                            "pointer": "/acc",
                            "op": ">=",
                            "target": 0.5,
                            "holdout": {"a": 1},
                            "holdout_digest": "abc",
                            "holdout_output": "labels",
                            "timeout_s": 5.0,
                            "memory_mb": 64,
                            "max_output_bytes": 1000,
                            "pids_max": 16,
                            "rlimit_nproc": 16,
                            "reap_timeout_s": 1.0,
                            "output_drain_join_s": 1.0,
                        },
                    )
                ]
            )
        )


def test_production_catalog_without_plan_n_markers_loads_and_freezes():
    """Freeze-only: prod catalog + PLAN-n skip ids → empty freeze map.

    Full expand_trigger / resume e2e lives in
    ``test_expand_trigger_prod_catalog.py`` (no ``co_varnames`` proxy).
    """
    from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
    from coscientist.core.execution.domain import RequirementRef, TaskSpec
    from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA

    PLAN_N_FREEZE_SKIP_IDS = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)

    prod = _lc(
        _full_catalog(
            edition="production",
            templates=[
                _full_template(
                    id="artifact_present",
                    title="Artifact exists",
                    text_ru="есть",
                )
            ],
            defaults={"experiment": ["artifact_present"]},
        )
    )
    assert not any(t.id in PLAN_N_FREEZE_SKIP_IDS for t in prod.templates)
    skip_refs = [
        RequirementRef(template_id=tid) for tid in sorted(PLAN_N_FREEZE_SKIP_IDS)
    ]
    frozen = freeze_task_sandbox_limits(
        TaskSpec(
            task_key="PLAN-1",
            title="replan",
            goal="g",
            route="planner",
            criteria=skip_refs,
        ),
        prod,
    )
    assert frozen.sandbox_check_limits == {}
