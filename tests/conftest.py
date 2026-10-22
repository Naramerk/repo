import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

CORE_RULES = "coscientist.core.rules"   # required API: tests/README.md


@pytest.fixture(params=["refmodel", "core"])
def impl(request):
    """Implementation under test: the executable reference or the M0 core (skipped until it exposes the API)."""
    if request.param == "refmodel":
        import refmodel
        return refmodel
    return pytest.importorskip(CORE_RULES, reason=f"{CORE_RULES} not present yet (M0)")
