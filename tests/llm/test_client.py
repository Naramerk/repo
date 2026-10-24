"""M2 PR A1: thin LLM client — fake transport only (no network)."""

from __future__ import annotations

import ast
import asyncio
import json
import random
from pathlib import Path
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

import coscientist.llm as llm_package
from coscientist.llm import (
    ChatMessage,
    LlmClient,
    LlmConfig,
    LlmConfigError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmRequest,
    LlmResponseError,
    LlmTimeoutError,
    LlmTransportError,
    TransportResponse,
)


def _config(**overrides: Any) -> LlmConfig:
    base: dict[str, Any] = {
        "base_url": "https://example.test/v1",
        "api_key_env": "COSCI_TEST_LLM_KEY",
        "timeout_s": 1.0,
        "total_deadline_s": 30.0,
        "max_retries": 0,
        "retry_backoff_s": 0.0,
        "retry_backoff_multiplier": 2.0,
        "retry_backoff_jitter_s": 0.0,
        "retry_backoff_max_s": 60.0,
        "retry_on_timeout": False,
        "honor_retry_after": True,
        "retryable_status_codes": (503, 429),
        "max_completion_tokens": 64,
        "accepted_finish_reasons": ("stop",),
        "unexpected_transport_retryable": False,
    }
    base.update(overrides)
    return LlmConfig(**base)


def _ok_body(
    *,
    content: str = "hello",
    prompt_tokens: int = 3,
    completion_tokens: int = 2,
    model: str = "test-model",
    finish_reason: str = "stop",
) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return json.dumps(payload).encode("utf-8")


class FakeTransport:
    def __init__(self, outcomes: list[TransportResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> TransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        if not self.outcomes:
            raise AssertionError("FakeTransport has no outcomes left")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class HangingTransport:
    """Ignores timeout_s so asyncio.wait_for in LlmClient must cancel the call."""

    def __init__(self) -> None:
        self.calls = 0

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> TransportResponse:
        self.calls += 1
        await asyncio.sleep(3600)
        raise AssertionError("hang should have been cancelled")


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "secret-test-key")
    return "secret-test-key"


@pytest.mark.asyncio
async def test_complete_returns_usage_and_content(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=_ok_body(content="typed-out"))]
    )
    client = LlmClient(_config(), transport)
    req = LlmRequest(
        model="role-model-from-profile",
        messages=[ChatMessage(role="user", content="ping")],
    )
    resp = await client.complete(req)
    assert resp.content == "typed-out"
    assert resp.model == "test-model"
    assert resp.finish_reason == "stop"
    assert resp.usage.prompt_tokens == 3
    assert resp.usage.completion_tokens == 2
    assert resp.usage.latency_ms >= 0.0
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://example.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {api_key}"
    sent = json.loads(call["body"])
    assert sent["model"] == "role-model-from-profile"
    assert sent["max_tokens"] == 64
    assert sent["messages"] == [{"role": "user", "content": "ping"}]


@pytest.mark.asyncio
async def test_missing_api_key_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_TEST_LLM_KEY", raising=False)
    client = LlmClient(
        _config(),
        FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())]),
    )
    with pytest.raises(LlmConfigError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_empty_api_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "")
    client = LlmClient(
        _config(),
        FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())]),
    )
    with pytest.raises(LlmConfigError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_whitespace_only_api_key_is_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "   \t  ")
    client = LlmClient(
        _config(),
        FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())]),
    )
    with pytest.raises(LlmConfigError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_config_error_str_repr_never_include_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-live-super-secret-value-xyz"
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", secret)
    # Force a config error path that runs after env read would have the secret:
    # delete after client construction so complete() sees missing env.
    client = LlmClient(
        _config(),
        FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())]),
    )
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "   ")
    with pytest.raises(LlmConfigError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert secret not in str(ei.value)
    assert secret not in repr(ei.value)
    assert secret not in ei.value.args[0]


@pytest.mark.asyncio
async def test_provider_error_not_retryable_by_default(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=500, headers={}, body=b'{"error":"no"}'),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    client = LlmClient(_config(max_retries=2, retryable_status_codes=(503,)), transport)
    with pytest.raises(LlmProviderError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.status_code == 500
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_retries_only_configured_status_codes(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"busy"),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="after-retry")),
        ]
    )
    client = LlmClient(
        _config(max_retries=1, retryable_status_codes=(503,), retry_backoff_s=0.0),
        transport,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "after-retry"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_retries_exhausted(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"a"),
            TransportResponse(status_code=503, headers={}, body=b"b"),
            TransportResponse(status_code=503, headers={}, body=b"c"),
        ]
    )
    client = LlmClient(
        _config(max_retries=2, retryable_status_codes=(503,), retry_backoff_s=0.0),
        transport,
    )
    with pytest.raises(LlmProviderError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.status_code == 503
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_429_honors_retry_after(api_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "0.25"},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client = LlmClient(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            retry_backoff_s=100.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=1000.0,
        ),
        transport,
        rng=random.Random(0),
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_transport_retryable_flag_respected(api_key: str) -> None:
    transport = FakeTransport(
        [
            LlmTransportError("conn reset", retryable=True),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client = LlmClient(_config(max_retries=1, retry_backoff_s=0.0), transport)
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_transport_non_retryable_fails_immediately(api_key: str) -> None:
    transport = FakeTransport([LlmTransportError("tls", retryable=False)])
    client = LlmClient(_config(max_retries=3), transport)
    with pytest.raises(LlmTransportError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unexpected_transport_exception_wrapped(api_key: str) -> None:
    transport = FakeTransport([RuntimeError("boom")])
    client = LlmClient(_config(unexpected_transport_retryable=False), transport)
    with pytest.raises(LlmTransportError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.retryable is False
    assert ei.value.detail == "RuntimeError"


@pytest.mark.asyncio
async def test_timeout_retry_requires_flag(api_key: str) -> None:
    transport = FakeTransport([LlmTimeoutError("slow")])
    client = LlmClient(_config(max_retries=2, retry_on_timeout=False), transport)
    with pytest.raises(LlmTimeoutError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_timeout_retries_when_enabled(api_key: str) -> None:
    transport = FakeTransport(
        [
            LlmTimeoutError("slow"),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="late")),
        ]
    )
    client = LlmClient(
        _config(max_retries=1, retry_on_timeout=True, retry_backoff_s=0.0),
        transport,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "late"


@pytest.mark.asyncio
async def test_wait_for_timeout_on_hanging_transport(api_key: str) -> None:
    transport = HangingTransport()
    client = LlmClient(
        _config(timeout_s=0.05, total_deadline_s=1.0, retry_on_timeout=False),
        transport,
    )
    with pytest.raises(LlmTimeoutError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_malformed_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b'{"choices":[]}')]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmResponseError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_non_json_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b"not-json")]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmResponseError, match="not JSON"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_non_utf8_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b"\xff\xfe not utf-8")]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmResponseError, match="not UTF-8"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


@pytest.mark.asyncio
async def test_finish_reason_length_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={},
                body=_ok_body(content="cut", finish_reason="length"),
            )
        ]
    )
    client = LlmClient(_config(accepted_finish_reasons=("stop",)), transport)
    with pytest.raises(LlmFinishReasonError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.finish_reason == "length"
    assert ei.value.content == "cut"


@pytest.mark.asyncio
async def test_finish_reason_content_filter_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={},
                body=_ok_body(content="", finish_reason="content_filter"),
            )
        ]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmFinishReasonError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.finish_reason == "content_filter"


@pytest.mark.asyncio
async def test_missing_usage_tokens_rejected(api_key: str) -> None:
    body = {
        "model": "m",
        "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1},
    }
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200, headers={}, body=json.dumps(body).encode("utf-8")
            )
        ]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmResponseError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )


def test_config_requires_timeout_and_limits() -> None:
    with pytest.raises(ValidationError):
        LlmConfig(
            base_url="https://example.test",
            api_key_env="K",
            max_retries=0,
            retry_backoff_s=0.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=1.0,
            retry_on_timeout=False,
            honor_retry_after=False,
            retryable_status_codes=(),
            max_completion_tokens=1,
            accepted_finish_reasons=("stop",),
            unexpected_transport_retryable=False,
            total_deadline_s=1.0,
            # timeout_s missing
        )


def test_config_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValidationError):
        _config(timeout_s=0.0)


def test_request_requires_model_and_messages() -> None:
    with pytest.raises(ValidationError):
        LlmRequest(model="", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(ValidationError):
        LlmRequest(model="m", messages=[])


@pytest.mark.asyncio
async def test_provider_error_detail_does_not_echo_body(api_key: str) -> None:
    secret_body = b'{"error":"sk-leak-me"}'
    transport = FakeTransport(
        [TransportResponse(status_code=401, headers={}, body=secret_body)]
    )
    client = LlmClient(_config(), transport)
    with pytest.raises(LlmProviderError) as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert "sk-leak-me" not in str(ei.value)
    assert "sk-leak-me" not in ei.value.detail


def test_llm_package_does_not_import_core() -> None:
    root = Path(llm_package.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "coscientist.core" or alias.name.startswith(
                        "coscientist.core."
                    ):
                        offenders.append(f"{path.name}:{node.lineno}:import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "coscientist.core" or mod.startswith("coscientist.core."):
                    offenders.append(f"{path.name}:{node.lineno}:from {mod}")
                if mod == "coscientist" and any(
                    alias.name == "core" for alias in node.names
                ):
                    offenders.append(f"{path.name}:{node.lineno}:from coscientist import core")
    assert offenders == []
