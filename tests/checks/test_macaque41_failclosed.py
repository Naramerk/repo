"""macaque #41 closeout: per-item fail-closed regression tests (checks scope).

Scope: ``coscientist/checks`` + CodeCheck / template domain fields, the
TaskCreated v13→v14 upcaster, and the freeze / ``CatalogError`` handling in
``plan/{lifecycle,expand,foreach}``. Study / HitlPolicy / CheckProposal /
create_study freeze are intentionally out of scope (#38 / #39).
"""

from __future__ import annotations

import inspect
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import coscientist.checks.sandbox as sb
import coscientist.checks.sandbox_probe as probe
from coscientist.checks import catalog as catalog_mod
from coscientist.checks.catalog import (
    CATALOG_VERSION,
    CATALOG_VERSION_HISTORY,
    SUPPORTED_CATALOG_VERSIONS,
    load_catalog,
)
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.errors import CatalogError, SandboxInfraError
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    Catalog,
    CodeCheck,
    ForEachSpec,
    QuestionSpec,
    RequirementRef,
    RequirementTemplate,
    ReviewCheckMeasured,
    TaskOutcome,
    TaskState,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.checks.sandbox_test_limits import (
    TEST_CHECK_DRAIN_JOIN_S,
    TEST_CHECK_REAP_TIMEOUT_S,
    TEST_MAX_OUTPUT_BYTES,
    TEST_PIDS_MAX,
    TEST_RLIMIT_NPROC,
    TEST_UNIT_MAX_OUTPUT_BYTES,
    TEST_UNIT_MEMORY_MB,
    sandbox_limits,
)
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime
from tests.test_pr9_plan_expanded import _boot_plan, _task
from tests.test_pr10_foreach_quorum import (
    _boot_stages,
    _exp,
    _upsert_selected_hypotheses,
)

# --------------------------------------------------------------------------
# 1. upcaster: TaskCreated v13 → v14 (security toggles)
# --------------------------------------------------------------------------


def _tc_payload(row):
    return {"spec": {"task_key": "E1", "sandbox_check_limits": {"exec_ok": row}}}


def _full_row(**over):
    row = {
        "timeout_s": 1.0,
        "memory_mb": 64,
        "max_output_bytes": 1024,
        "pids_max": 16,
        "rlimit_nproc": 16,
        "reap_timeout_s": 5.0,
        "output_drain_join_s": 5.0,
        "allow_network": False,
        "allow_cgroup_degraded": True,
    }
    row.update(over)
    return row


def _v13_to_v14(payload):
    from coscientist.core.events.upcasters import (
        _task_created_v13_to_v14_sandbox_freeze as fn,
    )

    return fn(payload)


def test_task_created_v13_upcast_missing_toggles_is_catalog_error_not_value_error():
    with pytest.raises(CatalogError, match="missing_security_toggles") as ei:
        _v13_to_v14(_tc_payload({"timeout_s": 1.0}))
    assert not isinstance(ei.value, ValueError)
    with pytest.raises(CatalogError, match="missing_security_toggles"):
        _v13_to_v14(_tc_payload({"allow_network": False}))


def test_task_created_v13_upcast_bad_types_raise_catalog_error():
    for bad in ("no", 0, 1, None):
        with pytest.raises(CatalogError, match="bad_security_toggle"):
            _v13_to_v14(_tc_payload({"allow_network": bad, "allow_cgroup_degraded": False}))
    with pytest.raises(CatalogError, match="spec_must_be_object"):
        _v13_to_v14({"spec": "nope"})
    with pytest.raises(CatalogError, match="spec_required"):
        _v13_to_v14({})
    with pytest.raises(CatalogError, match="sandbox_check_limits_must_be_object"):
        _v13_to_v14({"spec": {"sandbox_check_limits": []}})
    with pytest.raises(CatalogError, match="freeze_row_not_object"):
        _v13_to_v14(_tc_payload("x"))


def test_task_created_v13_upcast_refuses_unfrozen_and_accepts_complete_rows():
    p = _v13_to_v14(_tc_payload(_full_row()))
    row = p["spec"]["sandbox_check_limits"]["exec_ok"]
    assert row["allow_network"] is False and row["allow_cgroup_degraded"] is True
    # Empty map → typed refuse at upcast (never defer to apply_frozen_limits).
    with pytest.raises(CatalogError, match="sandbox_check_limits_empty"):
        _v13_to_v14({"spec": {"sandbox_check_limits": {}}})
    # Missing / null map → typed refuse, never a quiet pass-through.
    with pytest.raises(CatalogError, match="sandbox_check_limits_required"):
        _v13_to_v14({"spec": {"task_key": "SPEC"}})
    with pytest.raises(CatalogError, match="sandbox_check_limits_required"):
        _v13_to_v14({"spec": {"sandbox_check_limits": None}})


def test_task_created_writer_is_14_and_upcaster_registered():
    from coscientist.core.events import events as events_mod
    from coscientist.core.events import upcasters as up

    assert events_mod.TASK_CREATED_SCHEMA_VERSION == 14
    assert 13 in up.UPCASTERS.get("TaskCreated", {})
    assert up.UPCASTERS["TaskCreated"][13] is up._task_created_v13_to_v14_sandbox_freeze


# --------------------------------------------------------------------------
# 1b. catalog version chain 1/2 → 3
# --------------------------------------------------------------------------


def _catalog_blob(**over):
    blob = {
        "catalog_version": CATALOG_VERSION,
        "edition": "t",
        "sources_sha256": {},
        "rules": [],
        "templates": [],
        "defaults": {},
        "code_output_names": [],
    }
    blob.update(over)
    return blob


def test_catalog_version_history_covers_chain_and_current():
    assert CATALOG_VERSION == 4
    assert set(CATALOG_VERSION_HISTORY) == {1, 2, 3, 4}
    assert CATALOG_VERSION_HISTORY[CATALOG_VERSION] == "current"
    assert SUPPORTED_CATALOG_VERSIONS == frozenset({4})
    import operator

    with pytest.raises(TypeError, match="item assignment"):
        operator.setitem(CATALOG_VERSION_HISTORY, 5, "x")


@pytest.mark.parametrize("old", [1, 2, 3])
def test_old_catalog_blob_refused_with_history_reason(old):
    with pytest.raises(
        CatalogError,
        match=rf"unsupported_catalog_version:{old}:.*history={CATALOG_VERSION_HISTORY[old]}",
    ):
        load_catalog(_catalog_blob(catalog_version=old), kind_registry=default_execution_registry())


def test_unknown_catalog_generation_refused_as_unknown():
    with pytest.raises(CatalogError, match="history=unknown_generation"):
        load_catalog(_catalog_blob(catalog_version=99), kind_registry=default_execution_registry())
    with pytest.raises(CatalogError, match="must_be_strict_int_got_str"):
        load_catalog(
            _catalog_blob(catalog_version="0.2.0"),
            kind_registry=default_execution_registry(),
        )


# --------------------------------------------------------------------------
# 2. sandbox: unverified guest → infra; OOM never FAILED
# --------------------------------------------------------------------------


def _leaf(tmp_path: Path, *, memory_max: str, procs: str) -> Path:
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text(memory_max)
    (leaf / "cgroup.procs").write_text(procs)
    return leaf


def _lim(**kw):
    return sandbox_limits(
        timeout_s=1.0,
        memory_mb=TEST_UNIT_MEMORY_MB,
        max_output_bytes=TEST_UNIT_MAX_OUTPUT_BYTES,
        **kw,
    )


def test_verify_guest_unverified_live_bwrap_is_infra_not_soft_false(tmp_path):
    leaf = _leaf(tmp_path, memory_max="67108864", procs="4242\n")
    with pytest.raises(SandboxInfraError, match="guest_cgroup_membership_unverified"):
        sb._verify_guest_in_cgroup(leaf, 4242, lim=_lim(), bwrap_exited=lambda: False)


def test_verify_guest_present_or_exited_is_verified(tmp_path):
    leaf = _leaf(tmp_path, memory_max="67108864", procs="4242\n4243\n")
    assert sb._verify_guest_in_cgroup(leaf, 4242, lim=_lim(), bwrap_exited=lambda: False) == (True, None)
    leaf2 = tmp_path / "leaf2"
    leaf2.mkdir()
    (leaf2 / "memory.max").write_text("67108864")
    (leaf2 / "cgroup.procs").write_text("")
    (leaf2 / "pids.peak").write_text("2\n")
    assert sb._verify_guest_in_cgroup(leaf2, 4242, lim=_lim(), bwrap_exited=lambda: True) == (True, None)


def test_verify_guest_without_leaf_is_degraded_only():
    assert sb._verify_guest_in_cgroup(None, 1, lim=_lim(), bwrap_exited=lambda: False) == (False, "OOM not detectable")


def test_run_sandbox_live_guest_missing_from_leaf_raises_infra(tmp_path, monkeypatch):
    """Fake cgroup never lists a guest pid → infra (guest killed, no FAILED)."""
    if not sb.bwrap_available() or not probe.probe_bwrap_hard(allow_network=False):
        pytest.skip("bwrap hard required")
    root = tmp_path / "cg"
    root.mkdir()
    (root / "cgroup.controllers").write_text("memory cpu pids")
    (root / "cgroup.subtree_control").write_text("")
    (root / "cgroup.procs").write_text("")
    monkeypatch.setattr(sb, "_cgroup_nest_root", lambda: root)
    monkeypatch.setattr(sb, "_cgroup_delegation_available", lambda: True)
    monkeypatch.setattr(sb, "_current_cgroup_path", lambda: root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        for name, body in (
            ("memory.max", "max"),
            ("cpu.max", "max 100000"),
            ("pids.max", "max"),
            ("cgroup.procs", ""),
            ("memory.events", "oom_kill 0\n"),
            ("cpu.stat", "usage_usec 0\n"),
        ):
            (self / name).write_text(body)

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    with pytest.raises(SandboxInfraError, match="guest_cgroup_membership_unverified"):
        sb.run_sandbox(
            ["/bin/sleep", "5"],
            limits=sandbox_limits(
                timeout_s=5.0,
                memory_mb=TEST_UNIT_MEMORY_MB,
                max_output_bytes=TEST_UNIT_MAX_OUTPUT_BYTES,
                allow_cgroup_degraded=False,
            ),
            inputs={},
            env={},
        )


def test_classify_cgroup_oom_kill_is_oom_status_not_nonzero(tmp_path):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.events").write_text("low 0\noom_kill 1\n")
    (leaf / "cpu.stat").write_text("usage_usec 1000\n")
    res = sb._classify(
        timed_out=False,
        overflow=False,
        proc=SimpleNamespace(returncode=-9),  # type: ignore[arg-type]
        cgroup_leaf=leaf,
        oom_detectable=True,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=0.1,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        lim=_lim(),
        cgroup_limits_held=True,
        cpu_usage_s=0.001,
    )
    assert res.status is sb.SandboxStatus.OOM
    assert res.status in sb.INFRA_STATUSES


@pytest.mark.parametrize(
    "status",
    [
        sb.SandboxStatus.OOM,
        sb.SandboxStatus.CRASH,
        sb.SandboxStatus.MALFORMED,
        sb.SandboxStatus.UNAVAILABLE,
        sb.SandboxStatus.BLOCKED,
    ],
)
def test_exec_runs_non_scientific_status_is_infra_never_failed(tmp_path, monkeypatch, status):
    from coscientist.checks.kinds import executable as ex
    from coscientist.core.execution.artifacts import LocalArtifactStore

    def fake_run_sandbox(argv, **kw):
        return sb.SandboxResult(
            status=status,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            reason=f"sandbox_unavailable:{status.value}",
            duration_s=0.01,
            cgroup_limits_held=True,
            net_isolation="hard",
            fs_isolation="hard",
            oom_detectable=True,
        )

    monkeypatch.setattr(ex, "run_sandbox", fake_run_sandbox)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5.0,
        memory_mb=TEST_UNIT_MEMORY_MB,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    with pytest.raises(SandboxInfraError):
        ex.evaluate_exec_runs(
            check,
            template_id="exec_ok",
            check_id="exec_ok#0",
            out=AgentTaskOutput(outputs={}),
            artifacts=LocalArtifactStore(tmp_path / "arts"),
            role="validity",
            attempt_id="st/E1#1",
        )


def _patch_proc_reads(monkeypatch, *, stat: str | None = None, children: str | None = None):
    real_read = Path.read_text
    real_is_file = Path.is_file

    def fake_read(self, *a, **k):
        if stat is not None and self.name == "stat" and self.parent.name.isdigit():
            return stat
        if children is not None and self.name == "children":
            return children
        return real_read(self, *a, **k)

    def fake_is_file(self):
        if children is not None and self.name == "children":
            return True
        return real_is_file(self)

    monkeypatch.setattr(Path, "read_text", fake_read)
    monkeypatch.setattr(Path, "is_file", fake_is_file)


def test_host_tree_cpu_malformed_stat_is_infra_not_skipped(monkeypatch):
    _patch_proc_reads(monkeypatch, stat="garbage-without-paren")
    with pytest.raises(SandboxInfraError, match="host_tree_cpu:bad_stat"):
        sb._host_tree_cpu_s(os.getpid())


def test_host_tree_cpu_malformed_children_token_is_infra(monkeypatch):
    _patch_proc_reads(monkeypatch, children="not-a-pid")
    with pytest.raises(SandboxInfraError, match="host_tree_cpu:bad_children_token"):
        sb._host_tree_cpu_s(os.getpid())


# --------------------------------------------------------------------------
# 3. magic literals → named constants / catalog config
# --------------------------------------------------------------------------


def test_executable_constants_come_from_catalog_module():
    from coscientist.checks.kinds import executable as ex

    assert ex.LOG_SNIPPET_BYTES is catalog_mod.LOG_SNIPPET_BYTES
    assert ex.BUILTIN_TESTS_PASS_ARGV is catalog_mod.BUILTIN_TESTS_PASS_ARGV
    assert ex.BUILTIN_METRIC_SCORER_ARGV is catalog_mod.BUILTIN_METRIC_SCORER_ARGV
    assert catalog_mod.BUILTIN_TESTS_PASS_ARGV == ("catalog:pytest",)
    assert catalog_mod.BUILTIN_METRIC_SCORER_ARGV == ("catalog:metric",)
    assert catalog_mod.LOG_SNIPPET_BYTES == 8000
    src = inspect.getsource(ex)
    assert "LOG_SNIPPET_BYTES =" not in src
    assert '"catalog:pytest"' not in src and '"catalog:metric"' not in src


def test_sandbox_limits_constants_come_from_catalog_module():
    from coscientist.checks import sandbox_limits as sl

    assert sl.FROZEN_LIMIT_KEYS is catalog_mod.FROZEN_LIMIT_KEYS
    assert "FROZEN_LIMIT_KEYS: " not in inspect.getsource(sl)
    assert catalog_mod.PROPOSED_CHECK_TEMPLATE_ID == "proposed_check"
    assert '"proposed_check"' not in inspect.getsource(sl)
    ids = [t.id for t in minimal_test_catalog().templates]
    assert catalog_mod.PROPOSED_CHECK_TEMPLATE_ID in ids


def test_freeze_always_captures_proposed_check_template_limits():
    from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
    from coscientist.core.execution.domain import TaskSpec

    spec = TaskSpec(task_key="E1", title="t", goal="g", route="experiment")
    frozen = freeze_task_sandbox_limits(spec, minimal_test_catalog())
    assert catalog_mod.PROPOSED_CHECK_TEMPLATE_ID in frozen.sandbox_check_limits


def test_limits_from_check_requires_every_argument_and_has_no_host_budget_default():
    from coscientist.core.events.errors import CatalogError

    assert not hasattr(sb, "_LIMITS_OMITTED")
    assert not hasattr(sb, "DEFAULT_REAP_TIMEOUT_S")
    base = dict(
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=1024,
        pids_max=8,
        rlimit_nproc=8,
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    with pytest.raises(TypeError):
        sb.limits_from_check(**base)  # type: ignore[call-arg]
    with pytest.raises(CatalogError, match="sandbox_limits_bad_type"):
        sb.limits_from_check(**base, reap_timeout_s=None, output_drain_join_s=None)
    lim2 = sb.limits_from_check(**base, reap_timeout_s=0.5, output_drain_join_s=0.25)
    assert lim2.reap_timeout_s == 0.5 and lim2.output_drain_join_s == 0.25
    assert lim2.term_grace_s == catalog_mod.SANDBOX_TERM_GRACE_S
    with pytest.raises((TypeError, CatalogError, ValueError)):
        sb.limits_from_check(**base, reap_timeout_s=True, output_drain_join_s=1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unexpected keyword"):
        sb.limits_from_check(  # type: ignore[call-arg]
            **base, reap_timeout_s=None, output_drain_join_s=None, term_grace_s=1.0
        )


def _capture_probe_script(monkeypatch) -> str:
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: "/bin/true")
    monkeypatch.setattr(probe, "_common_jail_flags", lambda **_k: [])
    seen: dict[str, str] = {}

    def fake_run(argv, **kw):
        work = Path(argv[argv.index("--bind") + 1])
        seen["script"] = (work / "probe.py").read_text(encoding="utf-8")
        seen["timeout"] = kw["timeout"]  # type: ignore[assignment]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    try:
        assert probe.probe_bwrap_hard(allow_network=False) is True
    finally:
        probe.probe_bwrap_hard.cache_clear()
    return seen["script"], seen["timeout"]  # type: ignore[return-value]


def test_probe_script_built_from_named_constants(monkeypatch):
    script, timeout = _capture_probe_script(monkeypatch)
    assert repr(probe.PROBE_NET_CANARY_HOST) in script
    assert f"{probe.PROBE_NET_CANARY_HOST!r},{probe.PROBE_NET_CANARY_PORT}" in script
    assert f"sys.exit({probe.PROBE_EXIT_HOST_VISIBLE})" in script
    assert f"sys.exit({probe.PROBE_EXIT_NET_UP})" in script
    assert f"settimeout({probe.PROBE_NET_CONNECT_TIMEOUT_S!r})" in script
    assert timeout == probe.PROBE_SUBPROCESS_TIMEOUT_S
    assert repr(next(p for p in probe.PROBE_HOST_CANARY_PATHS if Path(p).is_file())) in script
    src = inspect.getsource(probe.probe_bwrap_hard)
    assert "1.1.1.1" not in src and "/etc/hostname" not in src


def test_probe_fails_closed_without_any_host_canary(monkeypatch):
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: "/bin/true")
    monkeypatch.setattr(probe, "PROBE_HOST_CANARY_PATHS", ("/nonexistent/canary",))

    def boom(*_a, **_k):
        raise AssertionError("probe must not run without a canary")

    monkeypatch.setattr(probe.subprocess, "run", boom)
    try:
        with pytest.raises(SandboxInfraError, match="bwrap_probe_no_host_canary"):
            probe.probe_bwrap_hard(allow_network=False)
    finally:
        probe.probe_bwrap_hard.cache_clear()


def test_sandbox_effect_bad_or_empty_timeout_kind_refused():
    from coscientist.checks.kinds._util import sandbox_effect

    kw = dict(
        attempt_id="a1",
        check_id="c",
        art_id="art",
        trusted=False,
        net_isolation="hard",
        fs_isolation="hard",
        note=None,
        countable=False,
    )
    for bad in ("", "bogus_timeout"):
        with pytest.raises(CatalogError, match="sandbox_effect_bad_timeout_kind"):
            sandbox_effect(**kw, timeout_kind=bad)  # type: ignore[arg-type]
    eff = sandbox_effect(**kw, timeout_kind="wall_timeout")  # type: ignore[arg-type]
    assert eff["timeout_kind"] == "wall_timeout" and eff["countable"] is False


def test_sandbox_effect_blocked_extra_raises():
    from coscientist.checks.kinds._util import _EFFECT_BLOCKED_KEYS, sandbox_effect

    assert "countable" in _EFFECT_BLOCKED_KEYS
    for key in sorted(_EFFECT_BLOCKED_KEYS):
        with pytest.raises(CatalogError, match=f"sandbox_effect_blocked_extra:{key}"):
            sandbox_effect(
                attempt_id="a1",
                check_id="c",
                art_id="art",
                trusted=True,
                net_isolation="hard",
                fs_isolation="hard",
                note=None,
                countable=True,
                extra={key: True},
            )


def test_slot_filename_dot_names_refused():
    from coscientist.checks.kinds._util import slot_filename

    for bad in ("", ".", ".."):
        with pytest.raises(CatalogError):
            slot_filename(bad)
    assert slot_filename("a/b") == "a_b"


def test_pointer_get_empty_string_raises():
    from coscientist.checks.kinds._util import pointer_get

    with pytest.raises(ValueError, match="invalid json pointer: empty"):
        pointer_get({"a": 1}, "")
    assert pointer_get({"a": 1}, None) == {"a": 1}


def test_tests_pass_launcher_agent_module_without_spec_raises_in_guest():
    from coscientist.checks.kinds import executable as ex

    src = ex._tests_pass_launcher().decode()
    assert "raise ImportError" in src
    assert "spec is None or spec.loader is None:\n            continue" not in src


# --------------------------------------------------------------------------
# 4. expand / foreach / accept_plan: real catalog paths (no freeze patching)
# --------------------------------------------------------------------------


def _catalog_with_marker(*, exempt: bool) -> Catalog:
    base = minimal_test_catalog()
    marker = RequirementTemplate(
        id="exempt_marker",
        title="marker",
        text_ru="маркер",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        check=ReviewCheckMeasured(
            measured=True,
            reviewer_role="critic",
            question="q",
            max_calls=1,
            metric="exempt_marker",
            op="==",
            target="pass",
        ),
        role="validity",
        required=False,
        version=1,
        exempt_from_freeze=exempt,
    )
    return base.model_copy(update={"templates": [*base.templates, marker]})


def _catalog_without(template_id: str) -> Catalog:
    base = minimal_test_catalog()
    return base.model_copy(
        update={
            "templates": [t for t in base.templates if t.id != template_id],
            "defaults": {},
        }
    )


async def _open_study(tmp_path, clock, runtime_cls, sid, catalog, **kw):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=catalog)
    params = {
        "profile": "research",
        "request": "q",
        "catalog_version": 4,
        "deadline": clock.now() + timedelta(seconds=400),
        "sid": sid,
        "finalize_reserve_s": 5,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
        "inputs": [],
    }
    params.update(kw)
    await rt.create_study(**params)
    return rt


def _task_created_payload(events, task_key):
    for e in events:
        if e.type == "TaskCreated" and e.payload["spec"]["task_key"] == task_key:
            return e.payload
    raise AssertionError(f"no TaskCreated for {task_key}")


_BASE_KEYS = {"SPEC", "HYP", "LIT", "PLAN", "REPORT", "E0", "FE"}


def _new_task_keys(events):
    return [
        e.payload["spec"]["task_key"]
        for e in events
        if e.type == "TaskCreated" and e.payload["spec"]["task_key"] not in _BASE_KEYS
    ]


def _marker_task(key, depends_on):
    spec = _task(key=key, addresses=["Q1"], depends_on=depends_on)
    return spec.model_copy(
        update={
            "criteria": [
                RequirementRef(template_id="value_match"),
                RequirementRef(template_id="exempt_marker"),
            ]
        }
    )


@pytest.mark.asyncio
async def test_expand_exempt_from_freeze_template_freezes_successfully(tmp_path, clock, runtime_cls):
    from coscientist.core.plan.expand import expand_plan

    rt = await _open_study(
        tmp_path,
        clock,
        runtime_cls,
        "st_m41_exp_ok",
        _catalog_with_marker(exempt=True),
        max_expansions=3,
    )
    attach_executor(rt, tmp_path)
    await _boot_plan(
        rt,
        tasks=[_marker_task("E0", ["PLAN"])],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    pid = await expand_plan(
        rt,
        after_task="E0",
        added_tasks=[_marker_task("E1", ["E0"])],
        origin="human",
        assign_keys=True,
    )
    assert pid
    events = await rt.store.read_all()
    assert not any(e.type == "ExpansionRefused" for e in events)
    new_keys = _new_task_keys(events)
    assert len(new_keys) == 1
    limits = _task_created_payload(events, new_keys[0])["spec"]["sandbox_check_limits"]
    assert "value_match" in limits
    assert "exempt_marker" not in limits
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_expand_non_exempt_non_code_template_catalog_error_not_expand_crash(tmp_path, clock, runtime_cls):
    """Real catalog (marker not exempt, ReviewCheck) → CatalogError re-raised."""
    from coscientist.core.plan.expand import expand_plan

    cat = _catalog_with_marker(exempt=False)
    rt = await _open_study(tmp_path, clock, runtime_cls, "st_m41_exp_bad", cat, max_expansions=3)
    attach_executor(rt, tmp_path)
    await _boot_plan(
        rt,
        tasks=[_task(key="E0", addresses=["Q1"], depends_on=["PLAN"])],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    with pytest.raises(CatalogError, match="sandbox_limits_non_codecheck:exempt_marker"):
        await expand_plan(
            rt,
            after_task="E0",
            added_tasks=[_marker_task("E1", ["E0"])],
            origin="human",
            assign_keys=True,
        )
    events = await rt.store.read_all()
    assert not any(
        e.type == "ExpansionRefused" and "expand_crash" in str((e.payload or {}).get("detail")) for e in events
    )
    await close_runtime(rt)


async def _boot_foreach(rt, *, criteria_marker: bool):
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=True,
    )
    if criteria_marker:
        fe = fe.model_copy(
            update={
                "criteria": [
                    RequirementRef(template_id="value_match"),
                    RequirementRef(template_id="exempt_marker"),
                ]
            }
        )
    await rt.accept_plan(
        [e0, fe],
        questions=[QuestionSpec(id="Q1", text="why")],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)
    from coscientist.core.events.ids import task_id

    e0_tid = task_id(rt.study.study_id, "E0")
    rt.tasks[e0_tid] = rt.tasks[e0_tid].model_copy(update={"state": TaskState.DONE, "outcome": TaskOutcome.SUCCESS})
    return e0_tid


@pytest.mark.asyncio
async def test_foreach_exempt_from_freeze_template_freezes_successfully(tmp_path, clock, runtime_cls):
    from coscientist.core.plan.foreach import maybe_foreach_after_task_done

    rt = await _open_study(
        tmp_path,
        clock,
        runtime_cls,
        "st_m41_fe_ok",
        _catalog_with_marker(exempt=True),
        max_expansions=0,
    )
    attach_executor(rt, tmp_path)
    e0_tid = await _boot_foreach(rt, criteria_marker=True)
    done = await maybe_foreach_after_task_done(rt, e0_tid)
    assert done
    events = await rt.store.read_all()
    assert not any("foreach_exception" in str((e.payload or {}).get("detail")) for e in events)
    child_keys = _new_task_keys(events)
    assert len(child_keys) == 2
    for key in child_keys:
        limits = _task_created_payload(events, key)["spec"]["sandbox_check_limits"]
        assert "value_match" in limits and "exempt_marker" not in limits
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_unknown_template_after_catalog_swap_is_catalog_error(tmp_path, clock, runtime_cls):
    from coscientist.core.plan.foreach import maybe_foreach_after_task_done

    rt = await _open_study(
        tmp_path,
        clock,
        runtime_cls,
        "st_m41_fe_swap",
        minimal_test_catalog(),
        max_expansions=0,
    )
    attach_executor(rt, tmp_path)
    e0_tid = await _boot_foreach(rt, criteria_marker=False)
    rt.catalog = _catalog_without("value_match")
    with pytest.raises(CatalogError, match="sandbox_limits_unknown_template:value_match"):
        await maybe_foreach_after_task_done(rt, e0_tid)
    events = await rt.store.read_all()
    assert not any("foreach_exception" in str((e.payload or {}).get("detail")) for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_non_catalog_error_still_refused_not_raised(tmp_path, clock, runtime_cls, monkeypatch):
    """Intentional: non-Catalog/Lease errors during materialize → refuse + skip."""
    from coscientist.core.plan import foreach as fe_mod

    rt = await _open_study(
        tmp_path,
        clock,
        runtime_cls,
        "st_m41_fe_exc",
        minimal_test_catalog(),
        max_expansions=0,
    )
    attach_executor(rt, tmp_path)
    e0_tid = await _boot_foreach(rt, criteria_marker=False)

    async def boom(*_a, **_k):
        raise RuntimeError("materialize_boom")

    monkeypatch.setattr(fe_mod, "_materialize_one", boom)
    await fe_mod.maybe_foreach_after_task_done(rt, e0_tid)
    events = await rt.store.read_all()
    assert any("foreach_exception:RuntimeError" in str((e.payload or {}).get("detail")) for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_real_bad_criteria_is_plan_refused_with_catalog_detail(tmp_path, clock, runtime_cls):
    """Non-exempt ReviewCheck template in criteria → freeze CatalogError → PlanRefused."""
    rt = await _open_study(tmp_path, clock, runtime_cls, "st_m41_acc_bad", _catalog_with_marker(exempt=False))
    await _boot_stages(rt)
    pid = await rt.accept_plan(
        [_marker_task("E0", ["PLAN"])],
        questions=[QuestionSpec(id="Q1", text="why")],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused
    assert "sandbox_limits_non_codecheck:exempt_marker" in str(refused[0].payload.get("detail"))
    assert not any(e.type == "TaskCreated" and e.payload["spec"]["task_key"] == "E0" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_exempt_template_accepted_and_frozen(tmp_path, clock, runtime_cls):
    rt = await _open_study(tmp_path, clock, runtime_cls, "st_m41_acc_ok", _catalog_with_marker(exempt=True))
    await _boot_stages(rt)
    pid = await rt.accept_plan(
        [_marker_task("E0", ["PLAN"])],
        questions=[QuestionSpec(id="Q1", text="why")],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    assert pid
    events = await rt.store.read_all()
    limits = _task_created_payload(events, "E0")["spec"]["sandbox_check_limits"]
    assert "value_match" in limits and "exempt_marker" not in limits
    await close_runtime(rt)


def test_plan_freeze_sites_use_runtime_catalog_and_reraise_catalog_error():
    import importlib

    for modname in ("expand", "foreach"):
        mod = importlib.import_module(f"coscientist.core.plan.{modname}")
        src = inspect.getsource(mod)
        assert "catalog = runtime.catalog" in src
        assert 'getattr(runtime, "catalog"' not in src
        assert "except CatalogError" in src


def test_codecheck_toggles_required_strict_bool_and_catalog_explicit():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as ei:
        CodeCheck(kind="artifact_exists", output="main")  # type: ignore[call-arg]
    assert {e["loc"][0] for e in ei.value.errors()} == {
        "allow_network",
        "allow_cgroup_degraded",
    }
    plain = CodeCheck(
        kind="artifact_exists",
        output="main",
        allow_network=False,
        allow_cgroup_degraded=False,
    )
    assert plain.allow_network is False and plain.allow_cgroup_degraded is False
    for bad in ("false", 0, 1, None):
        with pytest.raises(ValidationError):
            CodeCheck(  # type: ignore[arg-type]
                kind="artifact_exists",
                output="main",
                allow_network=bad,
                allow_cgroup_degraded=False,
            )
        with pytest.raises(ValidationError):
            CodeCheck(  # type: ignore[arg-type]
                kind="artifact_exists",
                output="main",
                allow_network=False,
                allow_cgroup_degraded=bad,
            )
    row = {
        "id": "x",
        "title": "x",
        "text_ru": "x",
        "method_sources": ["m"],
        "rule_ids": ["R23"],
        "applies_to": ["experiment"],
        "role": "validity",
        "required": True,
        "version": 1,
        "uses_sesoi": False,
        "predicts_null": False,
        "predicted_direction": "two_sided",
        "exempt_from_freeze": False,
    }
    blob = _catalog_blob(
        rules=[{"rule_id": "R23", "source": "reviewed-rules", "excerpt": "x", "digest": "d"}],
        defaults={},
    )
    reg = default_execution_registry()
    no_toggle = {**row, "check": {"kind": "artifact_exists", "output": "main"}}
    with pytest.raises(CatalogError, match="allow_network_required"):
        load_catalog({**blob, "templates": [no_toggle]}, kind_registry=reg)
    half = {
        **row,
        "check": {"kind": "artifact_exists", "output": "main", "allow_network": False},
    }
    with pytest.raises(CatalogError, match="allow_cgroup_degraded_required"):
        load_catalog({**blob, "templates": [half]}, kind_registry=reg)
