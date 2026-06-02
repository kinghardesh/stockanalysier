"""Active protective-stop manager (self-healing, quantity-aware, trailing).

Acts like a risk manager rather than a bystander:

  - QUANTITY-AWARE: every open position is covered by a stop for its FULL size,
    not just "has a stop somewhere". Adding to a position re-arms the whole lot.
  - TRAILING: on a winner the stop ratchets UP toward (current - horizon band)
    to lock in profit. It only ever moves in the protective direction — never
    loosened below an existing stop (or below the proposal's protective floor).
  - SELF-HEALING: Alpaca bracket legs are TimeInForce.DAY and expire each
    session; this re-arms a GTC stop so protection persists.

Runs every `stop_guard_interval_minutes` through the trading day. NOT gated by
the kill switch — open positions must stay protected even when new entries halt.
"""
import logging
import time
from collections import defaultdict
from decimal import Decimal

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.execution.service import _alpaca
from app.models import Trade, TradeProposal
from app.models.enums import LONG_TERM, TradeStatus, horizon_bucket
from app.risk.sizing import LONG_STOP_DISTANCE_PCT, STOP_DISTANCE_PCT, reconcile_bracket

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


def _trail_band(horizon) -> Decimal:
    return LONG_STOP_DISTANCE_PCT if horizon_bucket(horizon) == LONG_TERM else STOP_DISTANCE_PCT




def _desired_stop(is_long, entry, current, prop_stop, prop_tgt, horizon, existing_level):
    """Ratcheted protective stop level — only ever moves in the protective
    direction (longs: up; shorts: down) and never below the proposal's floor or
    an existing stop. Validity vs. the current price is checked by the caller."""
    protective = reconcile_bracket(
        "buy" if is_long else "sell", entry, prop_stop, prop_tgt, horizon=horizon)[0]
    band = _trail_band(horizon)
    if is_long:
        trail = current * (Decimal(1) - band) if settings.stop_guard_trail_enabled else Decimal(0)
        desired = max(protective, trail, existing_level or Decimal(0))
    else:
        big = current * Decimal(100)
        trail = current * (Decimal(1) + band) if settings.stop_guard_trail_enabled else big
        desired = min(protective, trail, existing_level if existing_level is not None else big)
    return desired.quantize(_CENTS)


def run_once() -> dict:
    result = {"positions": 0, "protected_ok": 0, "armed": 0, "trailed": 0, "errors": 0}
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

    stops_by_sym = defaultdict(list)
    for o in open_orders:
        if "stop" in str(o.type).lower():
            stops_by_sym[o.symbol].append(o)

    step_pct = Decimal(str(settings.stop_guard_min_trail_step_pct))

    for p in positions:
        result["positions"] += 1
        sym = p.symbol
        try:
            qty = int(float(p.qty))
            if qty == 0:
                continue
            is_long = qty > 0
            entry = Decimal(str(p.avg_entry_price))
            # Alpaca validates stop prices against its OWN mark, so use that as
            # the reference (the live IEX trade can diverge and get placements
            # rejected as "stop must be below current price").
            current = Decimal(str(p.current_price or p.avg_entry_price))

            existing = stops_by_sym.get(sym, [])
            covered = sum(int(float(o.qty)) for o in existing)
            levels = [Decimal(str(o.stop_price)) for o in existing if o.stop_price is not None]
            raw_level = (max(levels) if is_long else min(levels)) if levels else None
            # Only ratchet against a stop on the CORRECT side of the price. An
            # above-market (long) stop is anomalous/stuck and must be replaced,
            # not ratcheted to.
            valid_existing = None
            if raw_level is not None and ((is_long and raw_level < current)
                                          or (not is_long and raw_level > current)):
                valid_existing = raw_level

            prop_stop, prop_tgt, horizon = _intended_levels(sym)
            desired = _desired_stop(is_long, entry, current, prop_stop, prop_tgt, horizon, valid_existing)
            # Cap to a valid resting level: sell stop below the mark, buy above.
            gap = current * _MIN_GAP
            desired = (min(desired, current - gap) if is_long
                       else max(desired, current + gap)).quantize(_CENTS)

            need_qty = covered != abs(qty)
            step = current * step_pct
            if valid_existing is None:
                need_trail = True                       # no valid stop -> establish one
            elif is_long:
                need_trail = desired > valid_existing + step
            else:
                need_trail = desired < valid_existing - step

            if not need_qty and not need_trail:
                result["protected_ok"] += 1
                continue

            # Don't act while any of this symbol's orders are mid-flight
            # (pending_cancel / pending_replace) — Alpaca rejects cancels and
            # placements then. Defer to a later cycle once they settle.
            if any("pending" in str(o.status).lower() for o in existing):
                log.info("stop_guard: %s has pending orders; deferring", sym)
                result["deferred"] = result.get("deferred", 0) + 1
                continue

            side = OrderSide.SELL if is_long else OrderSide.BUY
            # Consolidate to exactly one full-size GTC stop: cancel every existing
            # stop, wait for the shares to free up, then place one. (Replace can
            # leave a lingering duplicate, over-covering the position.)
            for o in existing:
                try:
                    client.cancel_order_by_id(o.id)
                except Exception:
                    log.warning("stop_guard: could not cancel %s stop %s", sym, str(o.id)[:8])
            for _ in range(8):  # up to ~4s for cancels to clear
                time.sleep(0.5)
                still = [o for o in client.get_orders(filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, limit=50, symbols=[sym]))
                    if "stop" in str(o.type).lower()]
                if not still:
                    break
            client.submit_order(StopOrderRequest(
                symbol=sym, qty=abs(qty), side=side,
                stop_price=float(desired), time_in_force=TimeInForce.GTC))

            if need_qty:
                result["armed"] += 1
            else:
                result["trailed"] += 1
            log.info("stop_guard: %s qty=%s stop %s -> %s (covered %s/%s)",
                     sym, abs(qty), existing_level, desired, covered, abs(qty))
        except Exception:
            log.exception("stop_guard: failed for %s", sym)
            result["errors"] += 1

    log.info("stop_guard: %s", result)
    return result
