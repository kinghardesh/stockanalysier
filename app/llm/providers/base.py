from abc import ABC, abstractmethod


class ProviderError(Exception):
    """Generic transport/upstream failure (timeout, 5xx, network)."""


class RateLimitError(ProviderError):
    """Provider's own rate limit was hit (e.g. HTTP 429)."""


class SchemaValidationError(ProviderError):
    """Response was non-JSON or did not match the requested schema."""


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate_structured(self, prompt: str, schema: dict, timeout: int = 30) -> dict:
        """Return a parsed dict that conforms to `schema`, or raise."""
