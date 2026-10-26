"""Runner registry (plan item 14 + item 13).

Registered today: ``fake`` (deterministic replay) and ``new`` (plan item 13 —
the live CoScientist CLI subprocess adapter; see ``bench/live_runner.py``). The
old-system adapter (plan item 12) is intentionally **not** registered — it
arrives with its own live-run recipe. Requesting it is a typed
``runner not registered`` refusal, never a silent stub.

``new`` requires an explicit runner config YAML (``--runner-config``; see
``bench/runners.example.yaml``): every CLI flag is config-declared, no defaults
are invented. ``fake`` takes no config — passing one is a typed error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bench.errors import BenchConfigError
from bench.fake import FakeRunner
from bench.live_runner import LiveRunner, LiveRunnerConfig
from bench.runner import BenchRunner

RunnerFactory = Callable[[Path | None], BenchRunner]


def fake_runner(config_path: Path | None) -> BenchRunner:
    if config_path is not None:
        raise BenchConfigError("runner 'fake' takes no --runner-config (remove the flag)")
    return FakeRunner()


def new_runner(config_path: Path | None) -> BenchRunner:
    if config_path is None:
        raise BenchConfigError(
            "runner 'new' requires --runner-config PATH "
            "(explicit LiveRunnerConfig; see bench/runners.example.yaml)"
        )
    return LiveRunner(LiveRunnerConfig.from_yaml_file(config_path))


_RUNNERS: dict[str, RunnerFactory] = {
    "fake": fake_runner,
    "new": new_runner,
}


def registered_runners() -> tuple[str, ...]:
    return tuple(sorted(_RUNNERS))


def build_runner(name: str, config_path: Path | None = None) -> BenchRunner:
    """Build the registered runner ``name``; typed-refuse unknown names."""
    try:
        factory = _RUNNERS[name]
    except KeyError:
        raise BenchConfigError(
            f"runner not registered: {name!r} (registered: {', '.join(registered_runners())})"
        ) from None
    return factory(config_path)
