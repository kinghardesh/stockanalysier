"""Daily OHLCV snapshot -> permanent daily_bars table.

Pulls recent daily bars per whitelist ticker from Alpaca's historical API and
upserts them (ON CONFLICT on ticker+date), so the job is idempotent and a short
lookback backfills any gaps from missed runs. This is the long-term company
price record used for future reference / backtesting.
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.whitelist import WHITELIST
from app.models import DailyBar

log = logging.getLogger(__name__)

_client: StockHistoricalDataClient | None = None


def _data_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    return _client


def _notna(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def _dec(v):
    return Decimal(str(v)) if _notna(v) else None


def _int(v):
    return int(v) if _notna(v) else None


def snapshot_daily_bars(tickers=None, lookback_days: int = 5) -> dict:
    tickers = list(tickers or WHITELIST)
    result = {"tickers": len(tickers), "bars_upserted": 0, "errors": 0}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days + 5)

    for symbol in tickers:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=start, end=end, feed=DataFeed.IEX,
            )
            df = _data_client().get_stock_bars(req).df
        except Exception:
            log.exception("daily bar fetch failed for %s", symbol)
            result["errors"] += 1
            continue
        if df is None or df.empty:
            continue

        df = df.reset_index()
        try:
            with SessionLocal() as db:
                for _, r in df.iterrows():
                    ts = r.get("timestamp")
                    bar_date = ts.date() if hasattr(ts, "date") else None
                    if bar_date is None:
                        continue
                    vals = dict(
                        ticker=symbol.upper(), bar_date=bar_date,
                        open=_dec(r.get("open")), high=_dec(r.get("high")),
                        low=_dec(r.get("low")), close=_dec(r.get("close")),
                        volume=_int(r.get("volume")), trade_count=_int(r.get("trade_count")),
                        vwap=_dec(r.get("vwap")),
                    )
                    if None in (vals["open"], vals["high"], vals["low"], vals["close"]):
                        continue
                    stmt = pg_insert(DailyBar).values(**vals)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ticker", "bar_date"],
                        set_={k: stmt.excluded[k] for k in
                              ("open", "high", "low", "close", "volume", "trade_count", "vwap")},
                    )
                    db.execute(stmt)
                    result["bars_upserted"] += 1
                db.commit()
        except Exception:
            log.exception("daily bar upsert failed for %s", symbol)
            result["errors"] += 1

    log.info("snapshot_daily_bars: %s", result)
    return result
