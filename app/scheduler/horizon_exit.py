"""Time-based auto-exits keyed off each trade's intended horizon.

Short-term trades carry a time stop; long-term trades do not:
  - intraday : force-closed at/after `intraday_eod_close_et` (default 15:55 ET)
               so nothing is carried overnight.
  - swing    : closed after `swing_max_hold_days` calendar days if neither the
               Alpaca stop nor the target has triggered first.
  - position : no time exit — rides its stop/target indefinitely.

"Is it open?" is answered by the live Alpaca position list (the local
`positions` table is unused); the horizon + entry time come from the
trades/trade_proposals tables. A short-lived Redis guard stops us from
re-submitting a close for a ticker whose market close order is still settling.
"""
import logging
from datetime import datetime, time as dtime

from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import redis_client
from app.execution import ExecutionService
from app.models import Trade, TradeProposal
from app.models.enums import TimeHorizon, TradeStatus
from app.scheduler.jobs import ET

log = logging.getLogger(__name__)

_GUARD_PREFIX = "horizon_exit:closing:"


def _parse_hhmm(s: str) -> dtime:
    try:
        hh, mm = s.split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        log.warning("bad intraday_eod_close_et=%r; defaulting to 15:55", s)
        return dtime(15, 55)


def _guard_key(ticker: str) -> str:
    return f"{_GUARD_PREFIX}{ticker.upper()}"


async def run_once(executor: ExecutionService | None = None) -> dict:
    result = {
        "open_positions": 0, "held": 0, "no_horizon": 0,
        "intraday_closed": 0, "swing_closed": 0, "errors": 0,
    }
    executor = executor or ExecutionService()

    try:
        positions = executor.list_positions()
    except Exception:
        log.exception("horizon_exit: could not list Alpaca positions; skipping pass")
        result["errors"] += 1
        return result

    if not positions:
        return result

    now_et = datetime.now(ET)
    eod_cutoff = _parse_hhmm(settings.intraday_eod_close_et)
    intraday_due = now_et.timetz().replace(tzinfo=None) >= eod_cutoff
    result["open_positions"] = len(positions)

    with SessionLocal() as db:
        for pos in positions:
            ticker = (pos.get("ticker") or "").upper()
            if not ticker:
                continue

            # Most recent live entry for this ticker — gives us horizon + entry time.
            row = db.execute(
                select(Trade, TradeProposal)
                .join(TradeProposal, Trade.proposal_id == TradeProposal.id)
                .where(
                    TradeProposal.ticker == ticker,
                    Trade.status.in_([TradeStatus.filled, TradeStatus.partial]),
                )
                .order_by(Trade.opened_at.desc())
                .limit(1)
            ).first()
            if row is None:
                # Position with no matching pipeline trade (e.g. manual). Leave it.
                continue

            trade, prop = row
            horizon = prop.time_horizon
            if horizon is None:
                result["no_horizon"] += 1
                continue

            reason = None
            if horizon == TimeHorizon.intraday and intraday_due:
                reason = "intraday_eod"
            elif horizon == TimeHorizon.swing:
                opened_et = trade.opened_at.astimezone(ET)
                held_days = (now_et.date() - opened_et.date()).days
                if held_days >= settings.swing_max_hold_days:
                    reason = "swing_max_hold"
            # position: no time exit.

            if reason is None:
                result["held"] += 1
                continue

            # Skip if we already fired a close for this ticker recently.
            if redis_client.get(_guard_key(ticker)):
                log.info("horizon_exit: %s close already in flight; skipping", ticker)
                continue

            log.info(
                "horizon_exit: closing %s qty=%s (%s, horizon=%s, opened=%s)",
                ticker, pos.get("qty"), reason, horizon.value, trade.opened_at,
            )
            try:
                order_id = await executor.close_position(ticker)
            except Exception:
                log.exception("horizon_exit: close failed for %s", ticker)
                result["errors"] += 1
                continue

            if order_id:
                # Guard expires after one sweep window + buffer so a genuinely
                # stuck position gets retried, but a settling one is left alone.
                ttl = settings.horizon_exit_interval_minutes * 60 + 120
                try:
                    redis_client.set(_guard_key(ticker), "1", ex=ttl)
                except Exception:
                    log.warning("horizon_exit: could not set close guard for %s", ticker)
                if reason == "intraday_eod":
                    result["intraday_closed"] += 1
                else:
                    result["swing_closed"] += 1
            else:
                result["errors"] += 1

    log.info("horizon_exit: %s", result)
    return result
