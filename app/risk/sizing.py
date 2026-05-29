import math
from decimal import Decimal

MAX_SINGLE_TICKER_PCT = Decimal("0.20")
RISK_PER_TRADE_PCT = Decimal("0.01")

# Fallback bracket distances when the LLM's proposed levels are unusable.
STOP_DISTANCE_PCT = Decimal("0.03")    # 3% protective stop
TARGET_DISTANCE_PCT = Decimal("0.05")  # 5% take-profit
# Sanity bands — an LLM-proposed level outside these distances from the live
# entry is treated as garbage (the model often hallucinates prices from stale
# training data, e.g. proposing a $170 stop on a stock now trading at $310).
MAX_STOP_DISTANCE_PCT = Decimal("0.15")    # reject stops > 15% from entry
MAX_TARGET_DISTANCE_PCT = Decimal("0.30")  # reject targets > 30% from entry


def size_position(equity: Decimal, entry: Decimal, stop: Decimal) -> int:
    distance = abs(entry - stop)
    if distance == 0 or entry == 0:
        return 0
    risk_budget = equity * RISK_PER_TRADE_PCT
    risk_size = math.floor(risk_budget / distance)
    exposure_cap = math.floor((equity * MAX_SINGLE_TICKER_PCT) / entry)
    return max(0, min(risk_size, exposure_cap))


def reconcile_bracket(side, entry, stop, target) -> tuple[Decimal, Decimal]:
    """Return (stop, target) that validly bracket `entry` for the trade side.

    A bracket BUY needs stop < entry < target; a SELL needs target < entry < stop.
    The LLM proposes stop/target blind to the live quote, so its levels can land
    on the wrong side of the current price — which Alpaca rejects. We keep a
    proposed level if it's already on the correct side; otherwise clamp it to a
    fixed percentage from the live entry.
    """
    side_str = side.value if hasattr(side, "value") else str(side)
    entry = Decimal(str(entry))
    stop = Decimal(str(stop)) if stop is not None else None
    target = Decimal(str(target)) if target is not None else None
    cents = Decimal("0.01")

    if side_str == "buy":
        # Valid buy stop: below entry, but no more than MAX_STOP_DISTANCE_PCT away.
        stop_floor = entry * (Decimal(1) - MAX_STOP_DISTANCE_PCT)
        if stop is None or not (stop_floor <= stop < entry):
            stop = (entry * (Decimal(1) - STOP_DISTANCE_PCT)).quantize(cents)
        # Valid buy target: above entry, but no more than MAX_TARGET_DISTANCE_PCT away.
        target_ceil = entry * (Decimal(1) + MAX_TARGET_DISTANCE_PCT)
        if target is None or not (entry < target <= target_ceil):
            target = (entry * (Decimal(1) + TARGET_DISTANCE_PCT)).quantize(cents)
    else:  # sell / short: stop above entry, target below
        stop_ceil = entry * (Decimal(1) + MAX_STOP_DISTANCE_PCT)
        if stop is None or not (entry < stop <= stop_ceil):
            stop = (entry * (Decimal(1) + STOP_DISTANCE_PCT)).quantize(cents)
        target_floor = entry * (Decimal(1) - MAX_TARGET_DISTANCE_PCT)
        if target is None or not (target_floor <= target < entry):
            target = (entry * (Decimal(1) - TARGET_DISTANCE_PCT)).quantize(cents)
    return stop, target
