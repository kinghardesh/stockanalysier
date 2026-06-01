import enum


class SignalSource(str, enum.Enum):
    sma_crossover = "sma_crossover"
    news = "news"
    filing = "filing"
    rsi_mean_reversion = "rsi_mean_reversion"


class ProposalSide(str, enum.Enum):
    buy = "buy"
    sell = "sell"


class ProposalTier(str, enum.Enum):
    tier_1 = "tier_1"
    tier_2 = "tier_2"
    tier_3 = "tier_3"


class TradeStatus(str, enum.Enum):
    pending = "pending"
    filled = "filled"
    partial = "partial"
    cancelled = "cancelled"
    rejected = "rejected"


class TradeSleeve(str, enum.Enum):
    trend = "trend"
    premium = "premium"  # reserved for Phase 5 — options (cash-secured puts)
    mean_reversion = "mean_reversion"
    discretionary = "discretionary"  # LLM-driven news/filing-derived trades


class RiskEventType(str, enum.Enum):
    rejection = "rejection"
    circuit_breaker = "circuit_breaker"
    kill_switch = "kill_switch"


class TimeHorizon(str, enum.Enum):
    """How long a trade is intended to be held.

    The LLM already proposes one of these per trade; persisting it lets the
    system apply horizon-aware bracket widths and time-based auto-exits.
    """
    intraday = "intraday"   # close before the bell, never carry overnight
    swing = "swing"         # hold a few days, time-stop if it stalls
    position = "position"   # long-term, rides stop/target with no time exit


# Two-bucket grouping surfaced on the dashboard.
SHORT_TERM = "short_term"
LONG_TERM = "long_term"


def horizon_bucket(h) -> str:
    """Map a TimeHorizon (or its string value, or None) to a bucket label.

    intraday + swing -> 'short_term'; position -> 'long_term'; unknown -> ''.
    Accepts the enum, its raw string value, or None so callers don't have to
    normalize first.
    """
    if h is None:
        return ""
    val = h.value if isinstance(h, TimeHorizon) else str(h)
    if val == TimeHorizon.position.value:
        return LONG_TERM
    if val in (TimeHorizon.intraday.value, TimeHorizon.swing.value):
        return SHORT_TERM
    return ""
