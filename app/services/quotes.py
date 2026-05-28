from decimal import Decimal
from threading import Lock

from alpaca.data.enums import DataFeed
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
    # IEX feed: free paper tier doesn't permit SIP. See trend.py for context.
    # IEX latest trade is the most-recent IEX-routed trade, typically <1 minute
    # old during regular hours — good enough for entry-price estimation.
    req = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
    trades = _data_client().get_stock_latest_trade(req)
    return Decimal(str(trades[ticker].price))
