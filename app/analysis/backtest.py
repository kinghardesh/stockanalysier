"""Event-based backtest of the mechanical screen strategy on warehoused daily
bars. For every historical signal it simulates the same bracket the live system
would place (reconciled stop/target) and walks forward bar-by-bar to see whether
the target or the stop is hit first (or it times out), giving a real hit rate
and expectancy — without waiting weeks of live trading.

One position at a time per ticker (non-overlapping trades). Same-day stop+target
is counted as a stop (conservative).

CAVEAT: the universe is *today's* liquid names, so this carries survivorship /
selection bias and is optimistic. Treat it as a sanity check, not a promise.
"""
import logging
from decimal import Decimal

import pandas as pd
from sqlalchemy import select

from app.core.config import settings
from app.core.db import engine
from app.models import DailyBar
from app.risk.sizing import reconcile_bracket
from app.services.universe import universe_symbols

log = logging.getLogger(__name__)


def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0.0, float("nan"))
    return 100 - (100 / (1 + rs))


def run_backtest(max_hold: int = 20) -> dict:
    syms = universe_symbols()
    if not syms:
        return {"error": "no universe"}
    df = pd.read_sql(
        select(DailyBar.ticker, DailyBar.bar_date, DailyBar.high, DailyBar.low, DailyBar.close)
        .where(DailyBar.ticker.in_(syms)).order_by(DailyBar.ticker, DailyBar.bar_date),
        engine,
    )
    ob, os_ = settings.screen_rsi_overbought, settings.screen_rsi_oversold
    trades = []

    for ticker, g in df.groupby("ticker"):
        g = g.reset_index(drop=True)
        n = len(g)
        if n < 220:
            continue
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()
        rsi = _rsi_series(close)

        i = 200
        while i < n - 1:
            c, s50, s200, r = close[i], sma50[i], sma200[i], rsi[i]
            ret3m = (c / close[i - 63] - 1.0) if i >= 63 else 0.0
            sig = stop_hint = None
            if pd.notna(s50) and pd.notna(s200) and c > s50 > s200 and ret3m > 0 and r <= ob:
                sig, stop_hint = "momentum", s50
            elif pd.notna(s200) and r < os_ and c > s200:
                sig, stop_hint = "mean_reversion", c * 0.95
            if sig is None:
                i += 1
                continue

            entry = c
            stop_d, target_d = reconcile_bracket(
                "buy", Decimal(str(entry)), Decimal(str(stop_hint)), None, horizon=None)
            stop, target = float(stop_d), float(target_d)

            outcome, ret, exit_i = None, None, None
            for j in range(i + 1, min(i + 1 + max_hold, n)):
                if low[j] <= stop:                      # stop checked first (conservative)
                    outcome, ret, exit_i = "stop", stop / entry - 1.0, j
                    break
                if high[j] >= target:
                    outcome, ret, exit_i = "target", target / entry - 1.0, j
                    break
            if outcome is None:
                exit_i = min(i + max_hold, n - 1)
                ret = close[exit_i] / entry - 1.0
                outcome = "timeout"

            trades.append({"signal": sig, "outcome": outcome, "ret": ret})
            i = exit_i + 1

    return _summarize(trades, max_hold)


def _summarize(trades: list[dict], max_hold: int) -> dict:
    def stats(ts):
        if not ts:
            return {"n": 0}
        rets = [t["ret"] for t in ts]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        tgt = sum(1 for t in ts if t["outcome"] == "target")
        stp = sum(1 for t in ts if t["outcome"] == "stop")
        to = sum(1 for t in ts if t["outcome"] == "timeout")
        gross_w = sum(w for w in wins)
        gross_l = -sum(l for l in losses)
        return {
            "n": len(ts),
            "win_rate_pct": round(len(wins) / len(ts) * 100, 1),
            "target_hit_pct": round(tgt / len(ts) * 100, 1),
            "expectancy_pct": round(sum(rets) / len(ts) * 100, 2),
            "avg_win_pct": round(sum(wins) / len(wins) * 100, 2) if wins else 0.0,
            "avg_loss_pct": round(sum(losses) / len(losses) * 100, 2) if losses else 0.0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "target": tgt, "stop": stp, "timeout": to,
        }

    return {
        "max_hold_days": max_hold,
        "overall": stats(trades),
        "momentum": stats([t for t in trades if t["signal"] == "momentum"]),
        "mean_reversion": stats([t for t in trades if t["signal"] == "mean_reversion"]),
    }
