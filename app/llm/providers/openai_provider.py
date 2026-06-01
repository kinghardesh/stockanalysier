"""OpenAI provider — used as the GPT tie-breaker / second-opinion verifier.

Not part of the proposal-generation chain (gemini -> ollama); the orchestrator
holds it separately and calls it only to verify borderline proposals.
"""
import asyncio
import json
import logging

import jsonschema

from app.core.config import settings
from app.llm.providers.base import (
    LLMProvider, ProviderError, RateLimitError, SchemaValidationError,
)

log = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.openai_api_key
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        self.model = model or settings.openai_model
        # Imported lazily so the package is only required when a key is present.
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(api_key=self.api_key)

    async def generate_structured(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        from openai import APIError, APITimeoutError, RateLimitError as OAIRateLimit

        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise ProviderError(f"openai timeout after {timeout}s") from e
        except APITimeoutError as e:
            raise ProviderError(f"openai timeout: {e}") from e
        except OAIRateLimit as e:
            raise RateLimitError(f"openai 429: {e}") from e
        except APIError as e:
            raise ProviderError(f"openai api error: {e}") from e
        except Exception as e:
            raise ProviderError(f"openai unexpected: {e}") from e

        try:
            content = (resp.choices[0].message.content or "").strip()
        except (AttributeError, IndexError) as e:
            raise SchemaValidationError(f"openai malformed response: {e}") from e
        if not content:
            raise SchemaValidationError("openai returned empty response")

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise SchemaValidationError(
                f"openai returned non-JSON: {e}; head={content[:200]!r}"
            ) from e

        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:
            raise SchemaValidationError(f"openai response failed schema: {e.message}") from e
        return data
