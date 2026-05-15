import asyncio
import json
import logging

import jsonschema
import ollama

from app.core.config import settings
from app.llm.providers.base import LLMProvider, ProviderError, SchemaValidationError

log = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None):
        self.host = host or settings.ollama_host
        self.model = model or settings.ollama_model
        self._client = ollama.AsyncClient(host=self.host)

    async def generate_structured(self, prompt: str, schema: dict, timeout: int = 120) -> dict:
        try:
            resp = await asyncio.wait_for(
                self._client.chat(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    format="json",
                    options={"temperature": 0.2},
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise ProviderError(f"ollama timeout after {timeout}s") from e
        except Exception as e:
            raise ProviderError(f"ollama error: {e}") from e

        content = ((resp.get("message") or {}).get("content") or "").strip()
        if not content:
            raise SchemaValidationError("ollama returned empty response")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise SchemaValidationError(
                f"ollama returned non-JSON: {e}; head={content[:200]!r}"
            ) from e

        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:
            raise SchemaValidationError(f"ollama response failed schema: {e.message}") from e
        return data
