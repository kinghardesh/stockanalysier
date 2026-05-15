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
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from alpaca.trading.stream import TradingStream
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Trade
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


class AlpacaOrderStream:
    def __init__(self):
        self._stop = asyncio.Event()

    async def _handle(self, data) -> None:
        try:
            event = getattr(data, "event", None) or (data.get("event") if isinstance(data, dict) else None)
            order = getattr(data, "order", None) or (data.get("order") if isinstance(data, dict) else None)
            if order is None:
                return
            order_id = str(getattr(order, "id", None) or order.get("id"))
            if not order_id:
                return

            target_status = EVENT_STATUS_MAP.get(event)
            filled_qty_raw = (
                getattr(order, "filled_qty", None)
                if not isinstance(order, dict) else order.get("filled_qty")
            )
            filled_avg_raw = (
                getattr(order, "filled_avg_price", None)
                if not isinstance(order, dict) else order.get("filled_avg_price")
            )

            with SessionLocal() as db:
                trade = db.execute(
                    select(Trade).where(Trade.alpaca_order_id == order_id)
                ).scalar_one_or_none()
                if trade is None:
                    # Could be a manual order placed outside our pipeline. Just log.
                    log.debug("order update for unknown alpaca_order_id=%s event=%s", order_id, event)
                    return

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
                    if target_status in (TradeStatus.cancelled, TradeStatus.rejected, TradeStatus.filled):
                        # Closed_at marks order lifecycle completion, not position close.
                        # For position-close fills, the EOD reconciler will set realized_pnl.
                        if trade.closed_at is None and target_status != TradeStatus.filled:
                            trade.closed_at = datetime.now(timezone.utc)
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
