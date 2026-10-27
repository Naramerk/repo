"""Typed LLM request/response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coscientist.llm.strict_schema import sanitize_strict_schema


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
    schema_: dict[str, object] = Field(alias="schema")

    def to_openai_payload(self) -> dict[str, object]:
        """Nested OpenAI ``response_format`` object (key ``schema``, not ``schema_``).

        The single wire boundary (sole caller: openai_compat ``_encode_request``).
        When ``strict`` is true the schema passes through
        ``sanitize_strict_schema``: OpenAI structured outputs apply recursively
        at every object level (every property of every object in ``required``,
        optionals as required + nullable ``anyOf[…, null]``), which raw pydantic
        schemas violate in their nested ``$defs`` graph (issue #265). The raw
        ``schema_`` attribute — embedded in system prompts and used by
        client-side acceptance — is never mutated.
        """
        schema = self.schema_
        if self.strict:
            schema = sanitize_strict_schema(schema)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": self.name,
                "strict": self.strict,
                "schema": schema,
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
