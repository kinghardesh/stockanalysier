"""Select the curated liquid scan/trade universe from the full asset list.

Eligibility (a strong liquidity proxy): tradable + marginable + shortable +
easy-to-borrow on a major exchange. Those eligible names are then ranked by
recent average dollar volume (close x volume) and the top `universe_size` are
flagged `in_universe`. The core whitelist is always included.
"""
import logging
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.whitelist import WHITELIST
from app.models import Asset

log = logging.getLogger(__name__)

_client: StockHistoricalDataClient | None = None


def _data_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    return _client


def _dollar_volumes(symbols: list[str]) -> dict[str, float]:
    """Avg daily dollar volume (close x volume) over ~10 calendar days."""
    out: dict[str, float] = {}
    if not symbols:
        return out
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=12)
    for i in range(0, len(symbols), 200):
        batch = symbols[i:i + 200]
        try:
            df = _data_client().get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=start, end=end, feed=DataFeed.IEX,
            )).df
        except Exception:
            log.warning("dollar-volume batch fetch failed (%d syms); skipping", len(batch))
            continue
        if df is None or df.empty:
            continue
        df = df.reset_index()
        if "symbol" not in df.columns:
            continue
        df["dv"] = df["close"] * df["volume"]
        for sym, grp in df.groupby("symbol"):
            try:
                out[str(sym)] = float(grp["dv"].mean())
            except Exception:
                pass
    return out


def select_universe(target: int | None = None) -> dict:
    target = target or settings.universe_size
    with SessionLocal() as db:
        eligible = db.execute(
            select(Asset.symbol).where(
                Asset.tradable.is_(True),
                Asset.marginable.is_(True),
                Asset.shortable.is_(True),
                Asset.easy_to_borrow.is_(True),
                Asset.exchange.in_(settings.universe_exchanges),
            )
        ).scalars().all()

    result = {"eligible": len(eligible), "ranked": 0, "selected": 0}
    if not eligible:
        log.warning("select_universe: no eligible assets (is the assets table synced?)")
        return result

    dv = _dollar_volumes(list(eligible))
    result["ranked"] = len(dv)
    ranked = sorted(dv.items(), key=lambda kv: kv[1], reverse=True)
    chosen = {sym for sym, _ in ranked[:target]}
    chosen |= {t.upper() for t in WHITELIST}   # always keep the core whitelist

    with SessionLocal() as db:
        db.execute(update(Asset).values(in_universe=False))
        db.execute(update(Asset).where(Asset.symbol.in_(chosen)).values(in_universe=True))
        db.commit()
    result["selected"] = len(chosen)
    log.info("select_universe: %s", result)
    return result


def universe_symbols() -> list[str]:
    with SessionLocal() as db:
        return list(db.execute(
            select(Asset.symbol).where(Asset.in_universe.is_(True)).order_by(Asset.symbol)
        ).scalars().all())
