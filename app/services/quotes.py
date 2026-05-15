from decimal import Decimal
from threading import Lock

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from app.core.config import settings

_client = None
_lock = Lock()


def _data_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = StockHistoricalDataClient(
                    settings.alpaca_api_key, settings.alpaca_api_secret
                )
    return _client


def latest_trade_price(ticker: str) -> Decimal:
    req = StockLatestTradeRequest(symbol_or_symbols=ticker)
    trades = _data_client().get_stock_latest_trade(req)
    return Decimal(str(trades[ticker].price))
