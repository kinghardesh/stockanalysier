import math
from decimal import Decimal

MAX_SINGLE_TICKER_PCT = Decimal("0.20")
RISK_PER_TRADE_PCT = Decimal("0.01")


def size_position(equity: Decimal, entry: Decimal, stop: Decimal) -> int:
    distance = abs(entry - stop)
    if distance == 0 or entry == 0:
        return 0
    risk_budget = equity * RISK_PER_TRADE_PCT
    risk_size = math.floor(risk_budget / distance)
    exposure_cap = math.floor((equity * MAX_SINGLE_TICKER_PCT) / entry)
    return max(0, min(risk_size, exposure_cap))
