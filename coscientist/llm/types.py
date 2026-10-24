"""Typed LLM request/response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class LlmRequest(BaseModel):
    """One chat-completion request. ``model`` is supplied by the caller (profile)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)


class JsonSchemaResponseFormat(BaseModel):
    """OpenAI-compatible ``response_format`` for strict JSON Schema outputs.

    Wire shape (via ``model_dump(mode="json", by_alias=True)`` after wrapping in
    encode): ``{"type":"json_schema","json_schema":{"name","strict","schema"}}``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    strict: bool
    schema_: dict[str, Any] = Field(alias="schema")

    def to_openai_payload(self) -> dict[str, Any]:
        """Nested OpenAI ``response_format`` object (key ``schema``, not ``schema_``)."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": self.name,
                "strict": self.strict,
                "schema": self.schema_,
            },
        }


class StructuredLlmRequest(LlmRequest):
    """Chat request with required OpenAI-compatible ``response_format`` (JSON schema)."""

    response_format: JsonSchemaResponseFormat


class Usage(BaseModel):
    """Token counts from the provider plus wall-clock latency of the client call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: float = Field(
        ge=0.0,
        description=(
            "Wall-clock milliseconds for LlmClient.complete including retries and backoff; "
            "when returned from ProviderAdapter.complete alone, milliseconds for that one attempt."
        ),
    )


class LlmResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    model: str = Field(min_length=1)
    finish_reason: str = Field(min_length=1)
    usage: Usage
