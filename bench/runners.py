"""Runner registry (plan item 14).

Only ``fake`` is registered today. The old-system adapter (plan item 12) and
the new-system Orchestrator adapter (plan item 13) are intentionally **not**
registered — they arrive with the live-run recipe. Requesting them is a typed
``runner not registered`` refusal, never a silent stub.
"""

from __future__ import annotations

from collections.abc import Callable

from bench.errors import BenchConfigError
from bench.fake import FakeRunner
from bench.runner import BenchRunner

RunnerFactory = Callable[[], BenchRunner]


def fake_runner() -> BenchRunner:
    return FakeRunner()


_RUNNERS: dict[str, RunnerFactory] = {
    "fake": fake_runner,
}


def registered_runners() -> tuple[str, ...]:
    return tuple(sorted(_RUNNERS))


def build_runner(name: str) -> BenchRunner:
    """Build the registered runner ``name``; typed-refuse unknown names."""
    try:
        factory = _RUNNERS[name]
    except KeyError:
        raise BenchConfigError(
            f"runner not registered: {name!r} (registered: {', '.join(registered_runners())})"
        ) from None
    return factory()
