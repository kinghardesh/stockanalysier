import asyncio
import json
import logging

import google.generativeai as genai
from google.api_core import exceptions as gcp_exc

from app.core.config import settings
from app.llm.providers.base import (
    LLMProvider, ProviderError, RateLimitError, SchemaValidationError,
)

log = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model_name: str = "gemini-2.5-flash"):
        self.api_key = api_key or settings.gemini_api_key
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        genai.configure(api_key=self.api_key)
        self.model_name = model_name
        self._model = genai.GenerativeModel(model_name)

    async def generate_structured(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        gen_config = genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        )
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    self._model.generate_content, prompt, generation_config=gen_config
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise ProviderError(f"gemini timeout after {timeout}s") from e
        except gcp_exc.ResourceExhausted as e:
            raise RateLimitError(f"gemini 429: {e}") from e
        except (gcp_exc.DeadlineExceeded, gcp_exc.ServiceUnavailable) as e:
            raise ProviderError(f"gemini transient: {e}") from e
        except gcp_exc.InvalidArgument as e:
            raise SchemaValidationError(f"gemini rejected schema/prompt: {e}") from e
        except gcp_exc.GoogleAPIError as e:
            raise ProviderError(f"gemini api error: {e}") from e
        except Exception as e:
            raise ProviderError(f"gemini unexpected: {e}") from e

        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            raise SchemaValidationError("gemini returned empty response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaValidationError(
                f"gemini returned non-JSON: {e}; head={text[:200]!r}"
            ) from e
