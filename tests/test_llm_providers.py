import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as gcp_exc

from app.llm.providers.base import (
    ProviderError, RateLimitError, SchemaValidationError,
)
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.ollama import OllamaProvider


VALID_PAYLOAD = {
    "ticker": "AAPL", "side": "buy", "proposed_size_pct": 0.01,
    "stop_price": 100.0, "target_price": 120.0, "thesis": "t",
    "confidence": 7, "invalidation_criteria": "c",
    "time_horizon": "swing", "news_event_ids": [],
}
SCHEMA = {"type": "object"}


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.gemini_api_key", "test-key")
    with patch("app.llm.providers.gemini.genai") as mock_genai:
        mock_model = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.GenerationConfig = lambda **kw: kw
        provider = GeminiProvider(api_key="test-key")
        provider._model = mock_model
        yield provider, mock_model


def test_gemini_success(gemini):
    provider, mock_model = gemini
    mock_model.generate_content.return_value = MagicMock(text=json.dumps(VALID_PAYLOAD))
    result = asyncio.run(provider.generate_structured("hi", SCHEMA))
    assert result == VALID_PAYLOAD


def test_gemini_429_raises_rate_limit(gemini):
    provider, mock_model = gemini
    mock_model.generate_content.side_effect = gcp_exc.ResourceExhausted("rate limit")
    with pytest.raises(RateLimitError):
        asyncio.run(provider.generate_structured("hi", SCHEMA))


def test_gemini_malformed_json_raises_schema_error(gemini):
    provider, mock_model = gemini
    mock_model.generate_content.return_value = MagicMock(text="not-json{")
    with pytest.raises(SchemaValidationError):
        asyncio.run(provider.generate_structured("hi", SCHEMA))


def test_gemini_empty_response_raises_schema_error(gemini):
    provider, mock_model = gemini
    mock_model.generate_content.return_value = MagicMock(text="")
    with pytest.raises(SchemaValidationError):
        asyncio.run(provider.generate_structured("hi", SCHEMA))


@pytest.fixture
def ollama_p(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ollama_host", "http://x")
    monkeypatch.setattr("app.core.config.settings.ollama_model", "test")
    with patch("app.llm.providers.ollama.ollama") as mock_ollama:
        mock_client = MagicMock()
        mock_client.chat = AsyncMock()
        mock_ollama.AsyncClient.return_value = mock_client
        provider = OllamaProvider()
        provider._client = mock_client
        yield provider, mock_client


def test_ollama_success(ollama_p):
    provider, client = ollama_p
    client.chat.return_value = {"message": {"content": json.dumps({"x": 1})}}
    schema = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}
    result = asyncio.run(provider.generate_structured("hi", schema))
    assert result == {"x": 1}


def test_ollama_malformed_json(ollama_p):
    provider, client = ollama_p
    client.chat.return_value = {"message": {"content": "garbage"}}
    with pytest.raises(SchemaValidationError):
        asyncio.run(provider.generate_structured("hi", {"type": "object"}))


def test_ollama_fails_schema_validation(ollama_p):
    provider, client = ollama_p
    client.chat.return_value = {"message": {"content": json.dumps({"y": 1})}}
    schema = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}
    with pytest.raises(SchemaValidationError):
        asyncio.run(provider.generate_structured("hi", schema))
