import math
from decimal import Decimal
from typing import Optional

from app.models.enums import LONG_TERM, SHORT_TERM, horizon_bucket

MAX_SINGLE_TICKER_PCT = Decimal("0.20")
RISK_PER_TRADE_PCT = Decimal("0.01")
# Every auto-executed bracket must offer at least this reward-to-risk ratio;
# if the target is closer than MIN_REWARD_RISK x the stop distance, we widen
# the target so the trade isn't structurally risking more than it can make.
MIN_REWARD_RISK = Decimal("1.5")

# Fallback bracket distances when the LLM's proposed levels are unusable.
STOP_DISTANCE_PCT = Decimal("0.03")    # 3% protective stop
TARGET_DISTANCE_PCT = Decimal("0.05")  # 5% take-profit
# Sanity bands — an LLM-proposed level outside these distances from the live
# entry is treated as garbage (the model often hallucinates prices from stale
# training data, e.g. proposing a $170 stop on a stock now trading at $310).
# Short-term (intraday/swing) trades get a tight 8% stop band; trades with no
# declared horizon (mechanical signals such as the SMA-200 trend stop) keep the
# original 15% band so a legitimately wide trend stop isn't clamped to nothing.
SHORT_MAX_STOP_DISTANCE_PCT = Decimal("0.08")   # short-term: reject stops > 8%
MAX_STOP_DISTANCE_PCT = Decimal("0.15")         # unknown-horizon: reject stops > 15%
MAX_TARGET_DISTANCE_PCT = Decimal("0.30")       # reject targets > 30% from entry

# Long-term (position) bracket widths + sanity bands.
LONG_STOP_DISTANCE_PCT = Decimal("0.08")     # 8% protective stop
LONG_TARGET_DISTANCE_PCT = Decimal("0.20")   # 20% take-profit
LONG_MAX_STOP_DISTANCE_PCT = Decimal("0.25")     # accept stops up to 25% from entry
LONG_MAX_TARGET_DISTANCE_PCT = Decimal("0.60")   # accept targets up to 60% from entry


def _bracket_bands(horizon) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (stop_pct, target_pct, max_stop_pct, max_target_pct) for a horizon."""
    bucket = horizon_bucket(horizon)
    if bucket == LONG_TERM:
        return (
            LONG_STOP_DISTANCE_PCT, LONG_TARGET_DISTANCE_PCT,
            LONG_MAX_STOP_DISTANCE_PCT, LONG_MAX_TARGET_DISTANCE_PCT,
        )
    if bucket == SHORT_TERM:
        return (
            STOP_DISTANCE_PCT, TARGET_DISTANCE_PCT,
            SHORT_MAX_STOP_DISTANCE_PCT, MAX_TARGET_DISTANCE_PCT,
        )
    # Unknown horizon (mechanical signals / legacy): original wide stop band.
    return (
        STOP_DISTANCE_PCT, TARGET_DISTANCE_PCT,
        MAX_STOP_DISTANCE_PCT, MAX_TARGET_DISTANCE_PCT,
    )


def size_position(
    equity: Decimal, entry: Decimal, stop: Decimal,
    max_position_pct: Optional[Decimal] = None,
) -> int:
    """Shares to buy, the smallest of three caps:

      1. risk cap     — lose at most RISK_PER_TRADE_PCT of equity at the stop
      2. exposure cap — at most MAX_SINGLE_TICKER_PCT of equity in one name
      3. proposed cap — at most `max_position_pct` of equity (the size the LLM
                        actually asked for), when provided

    Pass the *reconciled* stop so the share count matches the stop the bracket
    order will really use.
    """
    distance = abs(entry - stop)
    if distance == 0 or entry == 0:
        return 0
    risk_budget = equity * RISK_PER_TRADE_PCT
    caps = [
        math.floor(risk_budget / distance),
        math.floor((equity * MAX_SINGLE_TICKER_PCT) / entry),
    ]
    if max_position_pct is not None and max_position_pct > 0:
        caps.append(math.floor((equity * Decimal(str(max_position_pct))) / entry))
    return max(0, min(caps))


def reconcile_bracket(side, entry, stop, target, horizon=None) -> tuple[Decimal, Decimal]:
    """Return (stop, target) that validly bracket `entry` for the trade side.

    A bracket BUY needs stop < entry < target; a SELL needs target < entry < stop.
    The LLM proposes stop/target blind to the live quote, so its levels can land
    on the wrong side of the current price — which Alpaca rejects. We keep a
    proposed level if it's already on the correct side AND within the horizon's
    sanity band; otherwise clamp it to the horizon's fixed percentage from the
    live entry. Long-term (position) trades get wider bands than short-term ones.
    """
    side_str = side.value if hasattr(side, "value") else str(side)
    entry = Decimal(str(entry))
    stop = Decimal(str(stop)) if stop is not None else None
    target = Decimal(str(target)) if target is not None else None
    cents = Decimal("0.01")
    stop_pct, target_pct, max_stop_pct, max_target_pct = _bracket_bands(horizon)

    if side_str == "buy":
        # Valid buy stop: below entry, but no more than max_stop_pct away.
        stop_floor = entry * (Decimal(1) - max_stop_pct)
        if stop is None or not (stop_floor <= stop < entry):
            stop = (entry * (Decimal(1) - stop_pct)).quantize(cents)
        # Valid buy target: above entry, but no more than max_target_pct away.
        target_ceil = entry * (Decimal(1) + max_target_pct)
        if target is None or not (entry < target <= target_ceil):
            target = (entry * (Decimal(1) + target_pct)).quantize(cents)
        # Enforce minimum reward:risk by widening the target if it's too close.
        min_target = entry + MIN_REWARD_RISK * (entry - stop)
        if target < min_target:
            target = min(min_target, target_ceil).quantize(cents)
    else:  # sell / short: stop above entry, target below
        stop_ceil = entry * (Decimal(1) + max_stop_pct)
        if stop is None or not (entry < stop <= stop_ceil):
            stop = (entry * (Decimal(1) + stop_pct)).quantize(cents)
        target_floor = entry * (Decimal(1) - max_target_pct)
        if target is None or not (target_floor <= target < entry):
            target = (entry * (Decimal(1) - target_pct)).quantize(cents)
        # Enforce minimum reward:risk by widening the (lower) target.
        min_target = entry - MIN_REWARD_RISK * (stop - entry)
        if target > min_target:
            target = max(min_target, target_floor).quantize(cents)
    return stop, target
