"""Remote coder CLI flag contract (slice-2 review fixes): explicit config or typed refuse."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from coscientist.cli.coder_remote import _remote_coder_settings_from_args
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import _validate_tool_cli_flags
from coscientist.tools.coder.remote_wire import RemoteLimits


def _args(**overrides: object) -> argparse.Namespace:
    fields: dict[str, object] = {
        "tools_json": Path("/tmp/none.json"),
        "mcp_tool_bundle": None,
        "tool_call_args": None,
        "coder_jobs": None,
        "coder_backend": "remote-openhands",
        "coder_remote_url": "http://10.32.2.2:8884",
        "coder_remote_header": None,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


class TestRemoteUrlRequired:
    def test_remote_backend_without_url_refuses(self) -> None:
        with pytest.raises(CliRefuse, match="requires --coder-remote-url") as ei:
            _remote_coder_settings_from_args(_args(coder_remote_url=None))
        assert ei.value.code == 2

    def test_remote_url_and_headers_without_remote_backend_refuse(self) -> None:
        with pytest.raises(CliRefuse, match="only valid with") as ei:
            _remote_coder_settings_from_args(
                _args(coder_backend="local-python", coder_remote_header=["Authorization: Bearer x"])
            )
        assert ei.value.code == 2
        with pytest.raises(CliRefuse, match="only valid with"):
            _remote_coder_settings_from_args(_args(coder_backend=None))

    def test_valid_settings_compose(self) -> None:
        settings = _remote_coder_settings_from_args(
            _args(coder_remote_header=["Authorization: Bearer abc", "X-Trace: t1"])
        )
        assert settings is not None
        assert settings.base_url == "http://10.32.2.2:8884"
        assert dict(settings.headers) == {"Authorization": "Bearer abc", "X-Trace": "t1"}
        assert isinstance(settings.limits, RemoteLimits)


class TestHeaderFormat:
    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",  # no colon
            "Authorization:",  # empty value
            ": Bearer x",  # empty name
            "Auth\tz: Bearer x",  # control char in name
            "Authorization: Bear\ner x",  # control char in value
        ],
    )
    def test_malformed_header_refuses(self, header: str) -> None:
        with pytest.raises(CliRefuse, match="must be 'NAME:VALUE' bare"):
            _remote_coder_settings_from_args(_args(coder_remote_header=[header]))

    def test_duplicate_header_name_refuses(self) -> None:
        with pytest.raises(CliRefuse, match="duplicate") as ei:
            _remote_coder_settings_from_args(
                _args(coder_remote_header=["Authorization: a", "Authorization: b"])
            )
        assert ei.value.code == 2


class TestCoderJobsRemote:
    def test_coder_jobs_with_remote_backend_refuses(self, tmp_path: Path) -> None:
        jobs = tmp_path / "jobs.json"
        jobs.write_text(json.dumps({"EXP-0": {}}), encoding="utf-8")
        with pytest.raises(CliRefuse, match="not valid with remote-openhands") as ei:
            _validate_tool_cli_flags(_args(coder_jobs=jobs))
        assert ei.value.code == 2


class TestContinueRemoteRefuse:
    @pytest.mark.asyncio
    async def test_continue_with_remote_backend_refuses(self, tmp_path: Path) -> None:
        """Slice-2 remote dispatch is run-only; a resumed study refuses typed."""
        import io

        from coscientist.checks.catalog import CATALOG_VERSION
        from coscientist.cli.continue_cmd import continue_study
        from coscientist.cli.static_router import StaticToolRouter
        from coscientist.profiles.load import load_profile
        from tests.checks.minimal_test_catalog import minimal_test_catalog
        from tests.cli.test_run_study import _default_tools

        profile = load_profile("research", expected_tool_catalog_version=2)
        with pytest.raises(CliRefuse, match="not supported on continue") as ei:
            await continue_study(
                data_root=tmp_path / "data",
                study_id="remote_continue_refuse",
                catalog=minimal_test_catalog(),
                loaded_profile=profile,
                owner="cli-test",
                adapter_factory=lambda policy: None,  # type: ignore[arg-type,return-value]
                out=io.StringIO(),
                catalog_version=CATALOG_VERSION,
                tools=StaticToolRouter(tools=_default_tools()),
                lease_ttl_s=60.0,
                lease_wait_s=5.0,
                max_llm_calls=10,
                max_tokens=10_000,
                max_tool_calls=5,
                max_ticks=5,
                coder_backend="remote-openhands",
            )
        assert ei.value.code == 2


class TestContinueParserRemoteFlags:
    def test_continue_parses_remote_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``continue`` declares both remote flags; the typed refusal proves the parse."""
        from coscientist.cli.run import main

        argv = [
            "continue",
            "/tmp/none-data",
            "--study-id",
            "remote_flags_parse",
            "--catalog",
            "/tmp/none-catalog.json",
            "--catalog-version",
            "1",
            "--profile",
            "research",
            "--tool-catalog-version",
            "2",
            "--owner",
            "cli-test",
            "--llm-config",
            "/tmp/none-llm.json",
            "--planner-model",
            "planner",
            "--executor-model",
            "executor",
            "--tz-spec-model",
            "tz",
            "--tools-json",
            "/tmp/none-tools.json",
            "--lease-ttl-s",
            "60",
            "--lease-wait-s",
            "5",
            "--rng-seed",
            "0",
            "--max-llm-calls",
            "10",
            "--max-tokens",
            "10000",
            "--max-tool-calls",
            "5",
            "--max-ticks",
            "5",
            "--coder-remote-url",
            "http://10.32.2.2:8884",
            "--coder-remote-header",
            "X-Trace: t1",
        ]
        with pytest.raises(SystemExit, match="2") as ei:
            main(argv)
        assert ei.value.code == 2
        assert "only valid with" in capsys.readouterr().err
