"""Position monitor — evaluates open positions against their stops/targets.

Reads `positions` table for currently-held tickers, fetches the latest trade
price for each, and submits a close order if the price has breached the stop
(any sleeve) or hit the target (where set). Updates `positions.last_evaluated_at`
on every pass regardless of outcome.

The actual close goes through ExecutionService.close_position() which submits
a market order. The order_stream consumer transitions the corresponding Trade
row to filled/cancelled and writes realized_pnl during the EOD reconcile.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.db import SessionLocal
from app.execution import ExecutionService
from app.models import Position
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, executor: ExecutionService | None = None):
        self.executor = executor or ExecutionService()

    async def run_once(self) -> dict:
        result = {"evaluated": 0, "stops_hit": 0, "targets_hit": 0, "errors": 0}
        with SessionLocal() as db:
            positions = db.execute(select(Position)).scalars().all()
            for pos in positions:
                result["evaluated"] += 1
                try:
                    price = latest_trade_price(pos.ticker)
                except Exception:
                    log.exception("price fetch failed for %s; skipping", pos.ticker)
                    result["errors"] += 1
                    continue

                action = self._evaluate(pos, price)
                pos.last_evaluated_at = datetime.now(timezone.utc)

                if action == "stop":
                    result["stops_hit"] += 1
                    await self._close(pos, price, reason="stop")
                elif action == "target":
                    result["targets_hit"] += 1
                    await self._close(pos, price, reason="target")

            db.commit()
        log.info("position_monitor: %s", result)
        return result

    @staticmethod
    def _evaluate(pos: Position, price: Decimal) -> str | None:
        """Return 'stop', 'target', or None.

        Long positions (qty > 0): stop fires when price <= current_stop;
        target fires when price >= current_target.
        Short positions (qty < 0): mirror semantics.
        """
        if pos.qty == 0:
            return None
        is_long = pos.qty > 0

        if pos.current_stop is not None:
            if is_long and price <= pos.current_stop:
                return "stop"
            if not is_long and price >= pos.current_stop:
                return "stop"

        if pos.current_target is not None:
            if is_long and price >= pos.current_target:
                return "target"
            if not is_long and price <= pos.current_target:
                return "target"
        return None

    async def _close(self, pos: Position, price: Decimal, reason: str) -> None:
        log.info(
            "%s hit on %s @ %s (qty=%s entry=%s stop=%s target=%s); closing",
            reason, pos.ticker, price, pos.qty, pos.avg_entry_price,
            pos.current_stop, pos.current_target,
        )
        try:
            await self.executor.close_position(pos.ticker)
        except Exception:
            log.exception("close failed for %s", pos.ticker)
