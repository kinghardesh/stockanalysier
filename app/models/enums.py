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
