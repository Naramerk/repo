from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    """Implementation under test: the executable reference or the M0 core."""
    if request.param == "refmodel":
        import refmodel

        return refmodel
    return pytest.importorskip(CORE_RULES, reason=f"{CORE_RULES} not present yet (M0)")


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
async def runtime(tmp_path: Path, clock: FakeClock):
    rt = await open_runtime(tmp_path, "st_test", owner="t1", clock=clock)
    yield rt
    await close_runtime(rt)


@pytest.fixture
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def short_deadline(clock: FakeClock):
    return clock.now() + timedelta(seconds=30)
