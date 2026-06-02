"""Protective-stop guard (safe, coverage-only).

Guarantees every open position has an active stop covering its full size. It is
deliberately CONSERVATIVE: it never cancels or replaces an existing stop — it
only *adds* a GTC stop for shares that are uncovered AND free. This avoids the
failure mode where cancelling a bracket's stop leg (whose shares are still held
by the take-profit leg) leaves a position naked.

Trailing (ratcheting stops up on winners) was removed because the cancel/replace
it required is unsafe against bracket OCO legs; it can be reintroduced later via
Alpaca-native trailing-stop orders, which the broker manages without cancels.

Runs every stop_guard_interval_minutes through the trading day. NOT gated by the
kill switch — open positions must stay protected even when entries are halted.
"""
import logging
from collections import defaultdict
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
_CENTS = Decimal("0.01")
_MIN_GAP = Decimal("0.001")  # a resting stop must sit at least 0.1% off the price


def _intended_levels(sym: str):
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


def _available(p) -> int:
    try:
        return abs(int(float(getattr(p, "qty_available", None) or p.qty)))
    except Exception:
        return abs(int(float(p.qty)))


def run_once() -> dict:
    result = {"positions": 0, "protected_ok": 0, "armed": 0, "no_free_qty": 0, "errors": 0}
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

    stop_cover = defaultdict(int)
    for o in open_orders:
        if "stop" in str(o.type).lower():
            try:
                stop_cover[o.symbol] += int(float(o.qty))
            except Exception:
                pass

    for p in positions:
        result["positions"] += 1
        sym = p.symbol
        try:
            qty = abs(int(float(p.qty)))
            covered = stop_cover.get(sym, 0)
            if covered >= qty:                      # already protected — never touch it
                result["protected_ok"] += 1
                continue

            free = _available(p)
            place_qty = min(qty - covered, free)
            if place_qty < 1:                       # uncovered shares are held by other orders
                result["no_free_qty"] += 1
                log.warning("stop_guard: %s uncovered (%d/%d) but no free qty to arm",
                            sym, covered, qty)
                continue

            is_long = float(p.qty) > 0
            entry = Decimal(str(p.avg_entry_price))
            current = Decimal(str(p.current_price or p.avg_entry_price))
            prop_stop, prop_tgt, horizon = _intended_levels(sym)
            stop = reconcile_bracket(
                "buy" if is_long else "sell", entry, prop_stop, prop_tgt, horizon=horizon)[0]
            gap = current * _MIN_GAP
            stop = (min(stop, current - gap) if is_long
                    else max(stop, current + gap)).quantize(_CENTS)
            side = OrderSide.SELL if is_long else OrderSide.BUY

            client.submit_order(StopOrderRequest(
                symbol=sym, qty=place_qty, side=side,
                stop_price=float(stop), time_in_force=TimeInForce.GTC))
            result["armed"] += 1
            log.info("stop_guard: armed GTC stop %s %d@%s (covered %d/%d)",
                     sym, place_qty, stop, covered, qty)
        except Exception:
            log.exception("stop_guard: failed for %s", sym)
            result["errors"] += 1

    log.info("stop_guard: %s", result)
    return result
