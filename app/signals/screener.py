"""Mechanical screen over the curated universe -> ranked buy candidates.

Two long setups, computed from daily_bars:
  - momentum:       uptrend (close > SMA50 > SMA200) with positive 3-month
                    return. Score = % above the 200-day (trend strength).
  - mean_reversion: oversold (RSI14 < screen_rsi_oversold) but still above the
                    200-day (bounce in an uptrend, not a falling knife).
                    Score = how far below the oversold threshold.

Each universe name yields at most one candidate (momentum takes priority). The
combined list is ranked by score and persisted to screen_candidates for the
day; the LLM deep-dive (Phase 3) draws the top N from there.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models import DailyBar, ScreenCandidate
from app.services.universe import universe_symbols

log = logging.getLogger(__name__)


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if pd.notna(val) else 50.0


def _dec(v):
    return Decimal(str(round(float(v), 6))) if v is not None and pd.notna(v) else None


def run_screen() -> dict:
    syms = universe_symbols()
    result = {"universe": len(syms), "screened": 0, "momentum": 0,
              "mean_reversion": 0, "candidates": 0, "top": []}
    if not syms:
        return result

    cutoff = (datetime.now(timezone.utc) - timedelta(days=420)).date()
    stmt = (select(DailyBar.ticker, DailyBar.bar_date, DailyBar.close)
            .where(DailyBar.ticker.in_(syms), DailyBar.bar_date >= cutoff)
            .order_by(DailyBar.ticker, DailyBar.bar_date))
    df = pd.read_sql(stmt, engine)
    if df.empty:
        log.warning("run_screen: no bars for universe (run snapshot_universe_bars first)")
        return result

    rsi_th = settings.screen_rsi_oversold
    candidates = []
    for ticker, g in df.groupby("ticker"):
        closes = g["close"].astype(float).reset_index(drop=True)
        n = len(closes)
        if n < settings.screen_min_history:
            continue
        result["screened"] += 1
        close = closes.iloc[-1]
        sma50 = closes.rolling(50).mean().iloc[-1] if n >= 50 else None
        sma200 = closes.rolling(200).mean().iloc[-1] if n >= 200 else None
        rsi = _rsi(closes)
        ret3m = (close / closes.iloc[-63] - 1.0) if n >= 63 else 0.0

        signal = score = stop = None
        # Momentum: a healthy uptrend (close > SMA50 > SMA200, positive 3-month
        # return) that is NOT parabolic (RSI <= overbought). Rank by 3-month
        # return — a standard momentum factor — not raw distance above the
        # 200-day, which just surfaces the most blown-off names.
        if (sma50 and sma200 and close > sma50 > sma200 and ret3m > 0
                and rsi <= settings.screen_rsi_overbought):
            signal = "momentum"
            score = ret3m * 100.0          # 3-month return, %
            stop = sma50
        elif sma200 and rsi < rsi_th and close > sma200:
            signal = "mean_reversion"
            score = rsi_th - rsi
            stop = close * 0.95
        if signal is None:
            continue
        candidates.append({
            "ticker": ticker, "signal": signal, "score": round(float(score), 4),
            "close": close, "sma50": sma50, "sma200": sma200, "rsi": rsi, "stop": stop,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    result["candidates"] = len(candidates)
    result["momentum"] = sum(1 for c in candidates if c["signal"] == "momentum")
    result["mean_reversion"] = sum(1 for c in candidates if c["signal"] == "mean_reversion")

    today = date.today()
    with SessionLocal() as db:
        db.execute(delete(ScreenCandidate).where(ScreenCandidate.screen_date == today))
        for rank, c in enumerate(candidates, start=1):
            db.add(ScreenCandidate(
                screen_date=today, ticker=c["ticker"], signal=c["signal"],
                score=_dec(c["score"]), rank=rank, close=_dec(c["close"]),
                sma50=_dec(c["sma50"]), sma200=_dec(c["sma200"]),
                rsi=_dec(c["rsi"]), suggested_stop=_dec(c["stop"]),
            ))
        db.commit()

    result["top"] = [f"{c['ticker']}({c['signal'][:3]},{c['score']:.1f})"
                     for c in candidates[:10]]
    log.info("run_screen: %s", result)
    return result
