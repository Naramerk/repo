"""M2 PR A1: thin LLM client — fake transport / injected clock (no network, no real waits)."""

from __future__ import annotations

import ast
import asyncio
import json
import random
from contextlib import suppress
from datetime import datetime, timedelta, timezone
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
    LlmError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmRequest,
    LlmResponseError,
    LlmRetryAfterError,
    LlmTimeoutError,
    LlmTransportError,
    TransportResponse,
)

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imf_fixdate(when: datetime) -> str:
    """Build a strict IMF-fixdate (GMT) matching coscientist.llm.imf_fixdate."""
    if when.tzinfo is None:
        raise AssertionError("test helper requires aware datetime")
    utc = when.astimezone(timezone.utc)
    return (
        f"{_DAY_NAMES[utc.weekday()]}, {utc.day:02d} {_MONTHS[utc.month - 1]} "
        f"{utc.year:04d} {utc.hour:02d}:{utc.minute:02d}:{utc.second:02d} GMT"
    )


class FakeClock:
    def __init__(
        self,
        mono: float = 0.0,
        wall: datetime | None = None,
    ) -> None:
        self._mono = mono
        self._wall = wall if wall is not None else datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self._mono

    def wall_utc(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)


def _config(**overrides: Any) -> LlmConfig:
    base: dict[str, Any] = {
        "base_url": "https://example.test/v1",
        "api_key_env": "COSCI_TEST_LLM_KEY",
        "chat_completions_path": "chat/completions",
        "max_tokens_field": "max_tokens",
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
    def __init__(
        self,
        outcomes: list[TransportResponse | BaseException],
        *,
        clock: FakeClock | None = None,
        advance_s: float = 0.0,
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self._clock = clock
        self._advance_s = advance_s

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
        if self._clock is not None and self._advance_s != 0.0:
            self._clock.advance(self._advance_s)
        if not self.outcomes:
            raise AssertionError("FakeTransport has no outcomes left")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _make_client(
    config: LlmConfig,
    transport: FakeTransport,
    *,
    clock: FakeClock | None = None,
    rng: random.Random | None = None,
    wait_for_timeout: bool = False,
) -> tuple[LlmClient, FakeClock, list[float]]:
    clock = clock if clock is not None else FakeClock()
    sleeps: list[float] = []

    async def sleep(delay_s: float) -> None:
        sleeps.append(delay_s)
        clock.advance(delay_s)

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        if wait_for_timeout:
            task = asyncio.create_task(awaitable)
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise TimeoutError()
        return await awaitable

    client = LlmClient(
        config,
        transport,
        clock=clock,
        sleep=sleep,
        wait_for=wait_for,
        rng=rng if rng is not None else random.Random(0),
    )
    return client, clock, sleeps


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "secret-test-key")
    return "secret-test-key"


@pytest.mark.asyncio
async def test_complete_returns_usage_and_content(api_key: str) -> None:
    clock = FakeClock()
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=_ok_body(content="typed-out"))],
        clock=clock,
        advance_s=0.05,
    )
    client, _, _ = _make_client(_config(), transport, clock=clock)
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
    assert resp.usage.latency_ms == 50.0
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://example.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {api_key}"
    assert call["timeout_s"] == 1.0
    sent = json.loads(call["body"])
    assert sent["model"] == "role-model-from-profile"
    assert sent["max_tokens"] == 64
    assert sent["messages"] == [{"role": "user", "content": "ping"}]


@pytest.mark.asyncio
async def test_attempt_timeout_passed_to_transport(api_key: str) -> None:
    clock = FakeClock()
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"busy"),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ],
        clock=clock,
        advance_s=0.4,
    )
    client, _, _ = _make_client(
        _config(
            timeout_s=1.0,
            total_deadline_s=1.0,
            max_retries=1,
            retry_backoff_s=0.0,
            retryable_status_codes=(503,),
        ),
        transport,
        clock=clock,
    )
    await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert transport.calls[0]["timeout_s"] == 1.0
    assert transport.calls[1]["timeout_s"] == 0.6


@pytest.mark.asyncio
async def test_missing_api_key_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_TEST_LLM_KEY", raising=False)
    transport = FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())])
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmConfigError, match="missing or empty"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 0


@pytest.mark.asyncio
async def test_empty_api_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "")
    transport = FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())])
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmConfigError, match="missing or empty"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 0


@pytest.mark.asyncio
async def test_whitespace_only_api_key_is_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "   \t  ")
    transport = FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())])
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmConfigError, match="missing or empty"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 0


@pytest.mark.asyncio
async def test_config_error_str_repr_never_include_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-live-super-secret-value-xyz"
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", secret)
    transport = FakeTransport([TransportResponse(status_code=200, headers={}, body=_ok_body())])
    client, _, _ = _make_client(_config(), transport)
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "   ")
    with pytest.raises(LlmConfigError, match="missing or empty") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert secret not in str(ei.value)
    assert secret not in repr(ei.value)
    assert secret not in ei.value.args[0]
    assert len(transport.calls) == 0


@pytest.mark.asyncio
async def test_provider_error_not_retryable_by_default(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=500, headers={}, body=b'{"error":"no"}'),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=2, retryable_status_codes=(503,)), transport
    )
    with pytest.raises(LlmProviderError, match="HTTP 500") as ei:
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
    client, _, sleeps = _make_client(
        _config(max_retries=1, retryable_status_codes=(503,), retry_backoff_s=0.0),
        transport,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "after-retry"
    assert len(transport.calls) == 2
    assert sleeps == []


@pytest.mark.asyncio
async def test_retries_exhausted(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"a"),
            TransportResponse(status_code=503, headers={}, body=b"b"),
            TransportResponse(status_code=503, headers={}, body=b"c"),
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=2, retryable_status_codes=(503,), retry_backoff_s=0.0),
        transport,
    )
    with pytest.raises(LlmProviderError, match="HTTP 503") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.status_code == 503
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_429_honors_retry_after_seconds(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "2"},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            retry_backoff_s=100.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=1000.0,
            total_deadline_s=30.0,
        ),
        transport,
        rng=random.Random(0),
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert sleeps == [2.0]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_429_honors_retry_after_http_date(api_key: str) -> None:
    clock = FakeClock(wall=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    when = clock.wall_utc() + timedelta(seconds=3)
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": _imf_fixdate(when)},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            retry_backoff_s=100.0,
            retry_backoff_max_s=1000.0,
            total_deadline_s=30.0,
        ),
        transport,
        clock=clock,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert sleeps == [3.0]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_retry_after_past_http_date_is_zero_delay(api_key: str) -> None:
    """RFC 9110 §10.2.3: past Retry-After HTTP-date means delay 0."""
    clock = FakeClock(wall=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    when = clock.wall_utc() - timedelta(seconds=30)
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": _imf_fixdate(when)},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            retry_backoff_s=100.0,
            retry_backoff_max_s=1000.0,
        ),
        transport,
        clock=clock,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert sleeps == []
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_retry_after_http_date_without_timezone_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                # Six tokens but zone is not GMT (no silent UTC assumption).
                headers={"Retry-After": "Thu, 01 Jan 2026 12:00:03 UTC"},
                body=b"rate",
            )
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,), honor_retry_after=True),
        transport,
    )
    with pytest.raises(LlmRetryAfterError, match="GMT"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_retry_after_invalid_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "not-a-date-or-number"},
                body=b"rate",
            )
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,), honor_retry_after=True),
        transport,
    )
    with pytest.raises(LlmRetryAfterError, match="invalid"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "raw",
    ["0.25", "1e3", "1_000", "١"],  # ASCII non-1*DIGIT + Arabic-Indic digit
)
@pytest.mark.asyncio
async def test_retry_after_rejects_non_digit_delta_seconds(api_key: str, raw: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=429, headers={"Retry-After": raw}, body=b"rate")]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,), honor_retry_after=True),
        transport,
    )
    with pytest.raises(LlmRetryAfterError):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_non_retryable_status_ignores_broken_retry_after(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=401,
                headers={"Retry-After": "not-a-date-or-number"},
                body=b"no",
            )
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,), honor_retry_after=True),
        transport,
    )
    with pytest.raises(LlmProviderError, match="HTTP 401") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.retry_after_s is None
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_honor_retry_after_false_ignores_header(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "99"},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=False,
            retry_backoff_s=1.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=10.0,
        ),
        transport,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert sleeps == [1.0]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_retry_after_above_remaining_budget_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "10"},
                body=b"rate",
            )
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            timeout_s=1.0,
            total_deadline_s=5.0,
            retry_backoff_max_s=1000.0,
        ),
        transport,
    )
    with pytest.raises(LlmTimeoutError, match="total_deadline_s") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert isinstance(ei.value.__cause__, LlmProviderError)
    assert len(transport.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_after_not_clamped_to_backoff_max(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "8"},
                body=b"rate",
            ),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(429,),
            honor_retry_after=True,
            retry_backoff_s=1.0,
            retry_backoff_max_s=2.0,
            total_deadline_s=30.0,
        ),
        transport,
    )
    await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert sleeps == [8.0]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_exponential_backoff_grows_until_max(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"1"),
            TransportResponse(status_code=503, headers={}, body=b"2"),
            TransportResponse(status_code=503, headers={}, body=b"3"),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=3,
            retryable_status_codes=(503,),
            honor_retry_after=False,
            retry_backoff_s=1.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=4.0,
            total_deadline_s=60.0,
        ),
        transport,
    )
    await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_jitter_with_fixed_seed(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"1"),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    seed_rng = random.Random(123)
    expected = 1.0 * (2.0**0) + seed_rng.uniform(0.0, 0.5)
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(503,),
            honor_retry_after=False,
            retry_backoff_s=1.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.5,
            retry_backoff_max_s=100.0,
        ),
        transport,
        rng=random.Random(123),
    )
    await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert sleeps == [expected]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_jitter_hits_backoff_max_ceiling(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"1"),
            TransportResponse(status_code=200, headers={}, body=_ok_body()),
        ]
    )
    # seed=0 → uniform(0, 2) ≈ 1.688 → 3 + 1.688 > 4 → capped to 4.0
    client, _, sleeps = _make_client(
        _config(
            max_retries=1,
            retryable_status_codes=(503,),
            honor_retry_after=False,
            retry_backoff_s=3.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=2.0,
            retry_backoff_max_s=4.0,
        ),
        transport,
        rng=random.Random(0),
    )
    await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert sleeps == [4.0]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_deadline_exhausted_when_backoff_exceeds_remaining(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=503, headers={}, body=b"busy")]
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=2,
            retryable_status_codes=(503,),
            honor_retry_after=False,
            retry_backoff_s=10.0,
            retry_backoff_multiplier=2.0,
            retry_backoff_jitter_s=0.0,
            retry_backoff_max_s=10.0,
            timeout_s=1.0,
            total_deadline_s=5.0,
        ),
        transport,
    )
    with pytest.raises(LlmTimeoutError, match="total_deadline_s") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert isinstance(ei.value.__cause__, LlmProviderError)
    assert len(transport.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_deadline_exhausted_before_next_attempt(api_key: str) -> None:
    """client.py remaining<=0 at loop start after a prior retryable failure."""
    clock = FakeClock()
    transport = FakeTransport(
        [
            TransportResponse(status_code=503, headers={}, body=b"a"),
            TransportResponse(status_code=503, headers={}, body=b"b"),
        ],
        clock=clock,
        advance_s=0.9,
    )
    client, _, sleeps = _make_client(
        _config(
            max_retries=2,
            retryable_status_codes=(503,),
            honor_retry_after=False,
            retry_backoff_s=0.0,
            timeout_s=1.0,
            total_deadline_s=1.0,
        ),
        transport,
        clock=clock,
    )
    with pytest.raises(LlmTimeoutError, match="total_deadline_s") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert isinstance(ei.value.__cause__, LlmProviderError)
    # Two attempts consume 0.9+0.9; third loop iteration hits remaining<=0.
    assert len(transport.calls) == 2
    assert sleeps == []


@pytest.mark.asyncio
async def test_naive_wall_utc_rejected(api_key: str) -> None:
    clock = FakeClock(wall=datetime(2026, 1, 1, 12, 0, 0))  # naive
    when = datetime(2026, 1, 1, 12, 0, 3, tzinfo=timezone.utc)
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": _imf_fixdate(when)},
                body=b"rate",
            )
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,), honor_retry_after=True),
        transport,
        clock=clock,
    )
    with pytest.raises(LlmError, match="timezone-aware"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_transport_retryable_flag_respected(api_key: str) -> None:
    transport = FakeTransport(
        [
            LlmTransportError("conn reset", retryable=True),
            TransportResponse(status_code=200, headers={}, body=_ok_body(content="ok")),
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retry_backoff_s=0.0), transport
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "ok"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_transport_non_retryable_fails_immediately(api_key: str) -> None:
    transport = FakeTransport([LlmTransportError("tls", retryable=False)])
    client, _, _ = _make_client(_config(max_retries=3), transport)
    with pytest.raises(LlmTransportError, match="tls"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unexpected_transport_exception_wrapped(api_key: str) -> None:
    transport = FakeTransport([RuntimeError("boom")])
    client, _, _ = _make_client(
        _config(unexpected_transport_retryable=False), transport
    )
    with pytest.raises(LlmTransportError, match="RuntimeError") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.retryable is False
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_timeout_retry_requires_flag(api_key: str) -> None:
    transport = FakeTransport([LlmTimeoutError("slow")])
    client, _, _ = _make_client(
        _config(max_retries=2, retry_on_timeout=False), transport
    )
    with pytest.raises(LlmTimeoutError, match="slow"):
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
    client, _, _ = _make_client(
        _config(max_retries=1, retry_on_timeout=True, retry_backoff_s=0.0),
        transport,
    )
    resp = await client.complete(
        LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    )
    assert resp.content == "late"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_wait_for_timeout_on_hanging_transport(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=_ok_body())]
    )
    client, _, _ = _make_client(
        _config(timeout_s=0.05, total_deadline_s=1.0, retry_on_timeout=False),
        transport,
        wait_for_timeout=True,
    )
    with pytest.raises(LlmTimeoutError, match="timeout_s"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1
    assert transport.calls[0]["timeout_s"] == 0.05


@pytest.mark.asyncio
async def test_malformed_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b'{"choices":[]}')]
    )
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmResponseError, match="missing choices"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_non_json_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b"not-json")]
    )
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmResponseError, match="not JSON"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_non_utf8_response_is_typed_error(api_key: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=200, headers={}, body=b"\xff\xfe not utf-8")]
    )
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmResponseError, match="not UTF-8"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


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
    client, _, _ = _make_client(_config(accepted_finish_reasons=("stop",)), transport)
    with pytest.raises(LlmFinishReasonError, match="length") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.finish_reason == "length"
    assert ei.value.content == "cut"
    assert len(transport.calls) == 1


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
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmFinishReasonError, match="content_filter") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert ei.value.finish_reason == "content_filter"
    assert len(transport.calls) == 1


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
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmResponseError, match="completion_tokens"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_retry_after_header_raises(api_key: str) -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=429,
                headers={"Retry-After": "1", "retry-after": "2"},
                body=b"rate",
            )
        ]
    )
    client, _, _ = _make_client(
        _config(max_retries=1, retryable_status_codes=(429,)), transport
    )
    with pytest.raises(LlmResponseError, match="duplicate header"):
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert len(transport.calls) == 1


def test_config_requires_timeout_and_limits() -> None:
    with pytest.raises(ValidationError, match="timeout_s"):
        LlmConfig(
            base_url="https://example.test",
            api_key_env="K",
            chat_completions_path="chat/completions",
            max_tokens_field="max_tokens",
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
        )


def test_config_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValidationError, match="finite"):
        _config(timeout_s=0.0)


def test_config_timeout_must_be_le_deadline() -> None:
    with pytest.raises(ValidationError, match="timeout_s must be <="):
        _config(timeout_s=10.0, total_deadline_s=5.0)


def test_config_backoff_max_ge_backoff() -> None:
    with pytest.raises(ValidationError, match="retry_backoff_max_s"):
        _config(retry_backoff_s=5.0, retry_backoff_max_s=1.0)


def test_config_rejects_duplicate_retryable_codes() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        _config(retryable_status_codes=(503, 503))


def test_config_rejects_duplicate_accepted_finish_reasons() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        _config(accepted_finish_reasons=("stop", "stop"))


def test_config_rejects_2xx_retryable() -> None:
    with pytest.raises(ValidationError, match="2xx"):
        _config(retryable_status_codes=(200, 503))


def test_config_accepts_list_for_tuple_fields() -> None:
    cfg = _config(
        retryable_status_codes=[503, 429],
        accepted_finish_reasons=["stop"],
    )
    assert cfg.retryable_status_codes == (503, 429)
    assert cfg.accepted_finish_reasons == ("stop",)


def test_config_strict_rejects_string_for_float_timeout() -> None:
    with pytest.raises(ValidationError):
        _config(timeout_s="1.0")  # str, not float — strict=True


def test_request_requires_model_and_messages() -> None:
    with pytest.raises(ValidationError, match="model"):
        LlmRequest(model="", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(ValidationError, match="messages"):
        LlmRequest(model="m", messages=[])


@pytest.mark.asyncio
async def test_provider_error_detail_does_not_echo_body(api_key: str) -> None:
    secret_body = b'{"error":"sk-leak-me"}'
    transport = FakeTransport(
        [TransportResponse(status_code=401, headers={}, body=secret_body)]
    )
    client, _, _ = _make_client(_config(), transport)
    with pytest.raises(LlmProviderError, match="HTTP 401") as ei:
        await client.complete(
            LlmRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        )
    assert "sk-leak-me" not in str(ei.value)
    assert "sk-leak-me" not in ei.value.detail
    assert len(transport.calls) == 1


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
