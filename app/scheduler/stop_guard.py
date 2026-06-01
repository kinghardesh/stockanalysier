"""Self-healing protective-stop guard.

Alpaca bracket legs are submitted with TimeInForce.DAY, so the stop/target
orders expire at the end of each session — leaving open positions unprotected
the next day. This job re-arms a GTC stop for any open position that has no
active stop order, using the originating proposal's reconciled stop level (or a
horizon-appropriate fallback). GTC means the re-armed stop persists.

Runs regardless of the kill switch (the kill switch blocks NEW entries, but
existing positions must always stay protected).
"""
import logging
from decimal import Decimal

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.execution.service import _alpaca
from app.models import Trade, TradeProposal
from app.models.enums import TradeStatus
from app.risk.sizing import reconcile_bracket

log = logging.getLogger(__name__)


def _intended_levels(sym: str):
    """Most recent live proposal's (stop, target, horizon) for a ticker."""
    with SessionLocal() as db:
        row = db.execute(
            select(TradeProposal)
            .join(Trade, Trade.proposal_id == TradeProposal.id)
            .where(
                TradeProposal.ticker == sym,
                Trade.status.in_([TradeStatus.filled, TradeStatus.partial]),
            )
            .order_by(Trade.opened_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None, None, None
        return row.stop_price, row.target_price, row.time_horizon


def run_once() -> dict:
    result = {"positions": 0, "already_protected": 0, "armed": 0, "errors": 0}
    client = _alpaca()
    try:
        positions = client.get_all_positions()
    except Exception:
        log.exception("stop_guard: could not list positions")
        result["errors"] += 1
        return result
    if not positions:
        return result

    try:
        open_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=500))
    except Exception:
        log.exception("stop_guard: could not list open orders")
        result["errors"] += 1
        return result
    protected = {o.symbol for o in open_orders if "stop" in str(o.type).lower()}

    for p in positions:
        result["positions"] += 1
        sym = p.symbol
        if sym in protected:
            result["already_protected"] += 1
            continue
        try:
            qty = int(float(p.qty))
            if qty == 0:
                continue
            is_long = qty > 0
            entry = Decimal(str(p.avg_entry_price))
            prop_stop, prop_tgt, horizon = _intended_levels(sym)
            # reconcile expects the trade side; a short position protects with a
            # stop ABOVE entry and closes with a BUY.
            stop, _ = reconcile_bracket(
                "buy" if is_long else "sell", entry, prop_stop, prop_tgt, horizon=horizon,
            )
            close_side = OrderSide.SELL if is_long else OrderSide.BUY
            order = client.submit_order(StopOrderRequest(
                symbol=sym, qty=abs(qty), side=close_side,
                stop_price=float(stop), time_in_force=TimeInForce.GTC,
            ))
            result["armed"] += 1
            log.info("stop_guard: armed GTC stop for %s qty=%s @ %s (order=%s)",
                     sym, qty, stop, str(order.id)[:8])
        except Exception:
            log.exception("stop_guard: failed to arm stop for %s", sym)
            result["errors"] += 1

    log.info("stop_guard: %s", result)
    return result
