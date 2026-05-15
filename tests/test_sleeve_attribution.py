"""Regression: LLM proposals must be tagged sleeve='discretionary', not
hardcoded to 'trend'. Without this, EOD attribution silently mislabels every
LLM-driven trade and the 'did LLM add alpha?' query becomes meaningless.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.orchestrator import LLMOrchestrator, NewsEvent
from app.llm.providers.base import LLMProvider
from app.llm.rate_limiter import RedisRateLimiter
from app.models.enums import SignalSource, TradeSleeve
from app.services.sleeve_map import sleeve_for_signal_source


# --- helper map -----------------------------------------------------------

def test_sleeve_for_sma_crossover():
    assert sleeve_for_signal_source(SignalSource.sma_crossover) == TradeSleeve.trend


def test_sleeve_for_rsi_mean_reversion():
    assert sleeve_for_signal_source(SignalSource.rsi_mean_reversion) == TradeSleeve.mean_reversion


def test_sleeve_for_news_is_discretionary():
    assert sleeve_for_signal_source(SignalSource.news) == TradeSleeve.discretionary


def test_sleeve_for_filing_is_discretionary():
    assert sleeve_for_signal_source(SignalSource.filing) == TradeSleeve.discretionary


# --- orchestrator stamps sleeve ------------------------------------------

@pytest.fixture(autouse=True)
def _whitelist(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.whitelist_tickers",
                        ["AAPL", "MSFT"])
    monkeypatch.setattr("app.core.config.settings.llm_provider_chain",
                        ["gemini"])
    monkeypatch.setattr("app.llm.orchestrator.is_kill_switch_active", lambda: False)


def _state_dict():
    return {
        "equity": "100000.00", "buying_power": "100000.00",
        "sod_equity": "100000.00", "daily_pnl": "0.00", "daily_pnl_pct": "0.00",
        "kill_switch_status": "OFF",
        "open_positions": "none", "recent_fills": "none",
    }


class _MockProvider(LLMProvider):
    def __init__(self, responses):
        self.name = "gemini"
        self._responses = list(responses)

    async def generate_structured(self, prompt, schema, timeout=30):
        return self._responses.pop(0)


def test_news_proposal_stamped_with_discretionary_sleeve():
    proposal_payload = {
        "ticker": "AAPL", "side": "buy", "proposed_size_pct": 0.003,
        "stop_price": 180.0, "target_price": 210.0,
        "thesis": "test", "confidence": 7,
        "invalidation_criteria": "close < 175",
        "time_horizon": "swing", "news_event_ids": ["e1"],
    }
    provider = _MockProvider([{"proposals": [proposal_payload]}])
    rl = MagicMock(spec=RedisRateLimiter)
    rl.allow = AsyncMock(return_value=True)
    orch = LLMOrchestrator(
        providers=[provider], rate_limiter=rl,
        account_state_provider=_state_dict,
    )

    events = [NewsEvent(event_id="e1", ticker="AAPL",
                        title_sanitized="x", source="s")]
    results = asyncio.run(orch.analyze_news_batch(events))

    assert results[0] is not None
    assert results[0].sleeve == "discretionary", (
        f"expected sleeve=discretionary, got {results[0].sleeve!r}. "
        "Regression: LLM trades would be mislabeled as 'trend' in EOD attribution."
    )
