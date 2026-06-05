"""Alpaca trading WebSocket consumer: updates the trades table as orders fill.

Phase 4 — replaces the open-loop stub. Subscribes to Alpaca's trade-update
stream, and on each event, updates the matching Trade row by alpaca_order_id.
Reconnects with exponential backoff on disconnect.

States we care about:
  - new / accepted             -> stays 'pending'
  - partial_fill               -> 'partial', update filled_qty/filled_price
  - fill                       -> 'filled', set filled_qty/filled_price, opened_at if first fill
  - canceled / expired         -> 'cancelled', set closed_at
  - rejected                   -> 'rejected', set closed_at
  - replaced                   -> we don't currently replace, log warn if seen

Realized P&L fix: when a SELL fills (stop hit, target hit, or manual close) it
arrives with the stop/take-profit order ID — different from the original BUY
order ID stored in Trade. We fall back to a ticker-level lookup: find the most
recent open filled trade for that ticker, compute realized P&L, and close it.
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from alpaca.trading.stream import TradingStream
from sqlalchemy import desc, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Trade, TradeProposal
from app.models.enums import TradeStatus

log = logging.getLogger(__name__)

# Alpaca event keys -> internal status (or None to leave unchanged)
EVENT_STATUS_MAP: dict[str, TradeStatus | None] = {
    "new":          None,
    "accepted":     None,
    "partial_fill": TradeStatus.partial,
    "fill":         TradeStatus.filled,
    "canceled":     TradeStatus.cancelled,
    "expired":      TradeStatus.cancelled,
    "rejected":     TradeStatus.rejected,
    "done_for_day": None,
    "replaced":     None,
    "stopped":      TradeStatus.cancelled,
    "suspended":    None,
}


def _get(obj, key):
    return getattr(obj, key, None) if not isinstance(obj, dict) else obj.get(key)


class AlpacaOrderStream:
    def __init__(self):
        self._stop = asyncio.Event()

    async def _handle(self, data) -> None:
        try:
            event  = _get(data, "event")
            order  = _get(data, "order")
            if order is None:
                return
            order_id = str(_get(order, "id") or "")
            if not order_id:
                return

            target_status  = EVENT_STATUS_MAP.get(event)
            filled_qty_raw = _get(order, "filled_qty")
            filled_avg_raw = _get(order, "filled_avg_price")
            symbol         = str(_get(order, "symbol") or "").upper()
            side           = str(_get(order, "side") or "").lower()

            now = datetime.now(timezone.utc)

            with SessionLocal() as db:
                # Primary lookup: match by the original BUY order_id stored in Trade.
                trade = db.execute(
                    select(Trade).where(Trade.alpaca_order_id == order_id)
                ).scalar_one_or_none()

                if trade is None and event == "fill" and "sell" in side and symbol:
                    # SELL fill from a stop/target/close order — different order ID.
                    # Find the most recent open trade for this ticker and close it.
                    trade = db.execute(
                        select(Trade)
                        .join(TradeProposal, Trade.proposal_id == TradeProposal.id)
                        .where(
                            TradeProposal.ticker == symbol,
                            Trade.status.in_([TradeStatus.filled, TradeStatus.partial]),
                            Trade.closed_at.is_(None),
                        )
                        .order_by(desc(Trade.opened_at))
                        .limit(1)
                    ).scalar_one_or_none()

                    if trade is not None:
                        # Compute realized P&L: (exit_price - entry_price) * qty
                        try:
                            exit_px  = Decimal(str(filled_avg_raw)) if filled_avg_raw else None
                            exit_qty = Decimal(str(filled_qty_raw)) if filled_qty_raw else None
                            if exit_px and exit_qty and trade.filled_price:
                                trade.realized_pnl = (exit_px - trade.filled_price) * exit_qty
                        except Exception:
                            log.warning("could not compute realized_pnl for %s", symbol)
                        trade.closed_at = now
                        trade.status    = TradeStatus.filled
                        db.commit()
                        log.info("position closed: ticker=%s exit=%.2f realized_pnl=%s",
                                 symbol, float(filled_avg_raw or 0), trade.realized_pnl)
                    else:
                        log.debug("sell fill for unknown ticker=%s order=%s", symbol, order_id)
                    return

                if trade is None:
                    log.debug("order update for unknown alpaca_order_id=%s event=%s", order_id, event)
                    return

                # Update fill details on the matched trade.
                if filled_qty_raw is not None:
                    try:
                        trade.filled_qty = Decimal(str(filled_qty_raw))
                    except Exception:
                        pass
                if filled_avg_raw is not None:
                    try:
                        trade.filled_price = Decimal(str(filled_avg_raw))
                    except Exception:
                        pass
                if target_status is not None:
                    trade.status = target_status
                    if target_status in (TradeStatus.cancelled, TradeStatus.rejected):
                        if trade.closed_at is None:
                            trade.closed_at = now
                    elif target_status == TradeStatus.filled:
                        # BUY filled — record opened_at if not already set.
                        if trade.opened_at is None:
                            trade.opened_at = now
                db.commit()
                log.info("order update applied: trade=%s event=%s status=%s qty=%s @ %s",
                         trade.id, event, target_status, trade.filled_qty, trade.filled_price)
        except Exception:
            log.exception("failed to process order update")

    async def run_forever(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                stream = TradingStream(
                    settings.alpaca_api_key, settings.alpaca_api_secret, paper=True,
                )
                stream.subscribe_trade_updates(self._handle)
                log.info("alpaca trading stream connected")
                await asyncio.to_thread(stream.run)
                backoff = 1
            except Exception:
                log.exception("trading stream error; backing off %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stop.set()
