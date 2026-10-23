from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from coscientist.checks.catalog import minimal_test_catalog  # noqa: E402
from coscientist.core.artifacts import LocalArtifactStore  # noqa: E402
from coscientist.core.clock import FakeClock  # noqa: E402
from coscientist.core.graph import default_execution_registry  # noqa: E402
from coscientist.core.runtime_factory import close_runtime, open_runtime  # noqa: E402

CORE_RULES = "coscientist.core.rules"  # required API: tests/README.md


@pytest.fixture(params=["refmodel", "core"])
def impl(request):
    """Implementation under test: the executable reference or the M0 core.

    For ``core``, Graph/Claim come from refmodel via ``core_rules_adapter`` so
    verdict / validate_plan / cardinality exercise ``core.rules``. Paper dedupe
    (``add_paper``) is refmodel-only — see ``test_paper_dedupe_two_lits_same_doi``.
    """
    if request.param == "refmodel":
        import refmodel

        return refmodel
    pytest.importorskip(CORE_RULES, reason=f"{CORE_RULES} not present yet (M0)")
    from core_rules_adapter import make_core_impl

    return make_core_impl()


@pytest.fixture(params=["core", "experimental"], ids=["rt_core", "rt_exp"])
def runtime_kind(request) -> str:
    """Which StudyRuntime to open: plain core (no HITL) or experimental (+HITL)."""
    return request.param


@pytest.fixture
def runtime_cls(runtime_kind: str) -> type:
    if runtime_kind == "core":
        from coscientist.core.machine import StudyRuntime

        return StudyRuntime
    from coscientist.experimental.runtime import StudyRuntime

    return StudyRuntime


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def catalog():
    return minimal_test_catalog()


@pytest.fixture
def registry():
    return default_execution_registry()


@pytest.fixture
async def runtime(tmp_path: Path, clock: FakeClock, runtime_cls: type):
    rt = await open_runtime(
        tmp_path, "st_test", owner="t1", clock=clock, runtime_cls=runtime_cls
    )
    yield rt
    await close_runtime(rt)


@pytest.fixture
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def short_deadline(clock: FakeClock):
    # Must exceed default finalize_reserve_s (30).
    return clock.now() + timedelta(seconds=60)


async def open_runtime_for(
    kind_or_cls: str | type,
    data_root: Path | str,
    study_id: str,
    **kwargs: Any,
):
    """Helper for tests that call open_runtime directly with a runtime kind."""
    if isinstance(kind_or_cls, str):
        if kind_or_cls == "core":
            from coscientist.core.machine import StudyRuntime as cls
        else:
            from coscientist.experimental.runtime import StudyRuntime as cls
    else:
        cls = kind_or_cls
    return await open_runtime(data_root, study_id, runtime_cls=cls, **kwargs)
