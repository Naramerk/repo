# DeepSeek search-only research profile

`research-deepseek-litsearch` preserves the GLM search-only research wiring and
budgets, using the provider's explicit `deepseek-v4-flash` model ID for all roles.
The transport config is `bench/lab/glm/llm-deepseek.json`; secrets stay outside the
repository in the named environment variable. A model ID is provider-specific;
the transport must not silently strip prefixes or substitute another model.

Live diagnosis on 2026-10-26: AITunnel rejected `openai/deepseek-v4-flash` with
HTTP400 `not a valid model ID`, before any planner/executor output. Its `/models`
listed `deepseek-v4-flash`. Minimal plain and strict-json-schema chat completions
using that ID and production max_tokens32768 both returned200, stop, valid JSON.
This confirms provider compatibility, not scientific scenario readiness.

The historical executor `!{` symptom is separate; it was not the cause of this
HTTP400. Role-adapter validation/repair must still be exercised independently.
No scenario prompts, thresholds or scientific gates are changed by this profile.
Search-only literature is metadata retrieval, not proof that full papers were parsed.
