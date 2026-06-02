import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import (
    ProposalSide, ProposalTier, RiskEventType, SignalSource, TimeHorizon,
    TradeSleeve, TradeStatus,
)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
    source: Mapped[SignalSource] = mapped_column(
        SAEnum(SignalSource, name="signal_source", create_type=False), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    proposals: Mapped[list["TradeProposal"]] = relationship(back_populates="signal")


class TradeProposal(Base):
    __tablename__ = "trade_proposals"
    __table_args__ = (CheckConstraint("confidence BETWEEN 1 AND 10", name="ck_confidence_range"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    side: Mapped[ProposalSide] = mapped_column(
        SAEnum(ProposalSide, name="proposal_side", create_type=False), nullable=False
    )
    proposed_size_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    stop_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    target_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    model_used: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tier: Mapped[ProposalTier] = mapped_column(
        SAEnum(ProposalTier, name="proposal_tier", create_type=False), nullable=False
    )
    # Intended hold duration proposed by the LLM. Nullable because rows created
    # before migration 0004 (and mechanical signals that don't set it) have none.
    time_horizon: Mapped[Optional[TimeHorizon]] = mapped_column(
        SAEnum(TimeHorizon, name="time_horizon", create_type=False), nullable=True, index=True
    )
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )

    signal: Mapped["Signal"] = relationship(back_populates="proposals")
    trades: Mapped[list["Trade"]] = relationship(back_populates="proposal")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    alpaca_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[TradeStatus] = mapped_column(
        SAEnum(TradeStatus, name="trade_status", create_type=False), nullable=False
    )
    filled_qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    filled_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    sleeve: Mapped[TradeSleeve] = mapped_column(
        SAEnum(TradeSleeve, name="trade_sleeve", create_type=False), nullable=False
    )
    model_used: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    proposal: Mapped["TradeProposal"] = relationship(back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    current_stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    current_target: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sleeve: Mapped[TradeSleeve] = mapped_column(
        SAEnum(TradeSleeve, name="trade_sleeve", create_type=False), nullable=False
    )


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
    event_type: Mapped[RiskEventType] = mapped_column(
        SAEnum(RiskEventType, name="risk_event_type", create_type=False), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    related_proposal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_proposals.id", ondelete="SET NULL"), nullable=True
    )
    account_state_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class DailySummary(Base):
    __tablename__ = "daily_summary"

    trading_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    starting_equity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    ending_equity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    mechanical_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, server_default=text("0"))
    llm_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, server_default=text("0"))
    by_model: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    by_sleeve: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    proposals_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    proposals_executed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    proposals_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DailyBar(Base):
    """Permanent daily OHLCV history per ticker — the long-term company record
    used for future reference / backtesting. Upserted by the daily snapshot job.
    """
    __tablename__ = "daily_bars"
    __table_args__ = (Index("ix_daily_bars_bar_date", "bar_date"),)

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    bar_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    trade_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    vwap: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CompanyFundamentals(Base):
    """Permanent latest-snapshot fundamentals per ticker — refreshed periodically
    so the LLMs have company context (sector, valuation, earnings) to reason over.
    """
    __tablename__ = "company_fundamentals"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    market_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 2), nullable=True)
    pe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    forward_pe: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    eps: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    dividend_yield: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    beta: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    fifty_two_week_high: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    fifty_two_week_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    next_earnings_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MarketData(Base):
    """Hot intraday market events (bars + trades) drained from the live stream.

    Kept for `market_data_retention_days` (default 30); the nightly archive job
    exports older rows to a compressed secondary store and prunes them here.
    """
    __tablename__ = "market_data"
    __table_args__ = (Index("ix_market_data_ticker_time", "ticker", "event_time"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # 'bar' | 'trade'
    event_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )


class Asset(Base):
    """The full Alpaca-tradable universe (synced periodically). Symbol-level
    reference metadata — what's tradable, on which exchange, shortable, etc."""
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_exchange", "exchange"),
        Index("ix_assets_tradable", "tradable"),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    asset_class: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    tradable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    marginable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    shortable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    easy_to_borrow: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fractionable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # True for the curated liquid scan/trade universe (top ~N by dollar volume).
    in_universe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ScreenCandidate(Base):
    """Output of the daily mechanical screen over the curated universe — a
    ranked list of buy candidates the LLM deep-dive (Phase 3) draws from."""
    __tablename__ = "screen_candidates"
    __table_args__ = (Index("ix_screen_candidates_date_rank", "screen_date", "rank"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    screen_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    signal: Mapped[str] = mapped_column(String(20), nullable=False)  # momentum | mean_reversion
    score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    sma50: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    sma200: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    rsi: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    suggested_stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ShadowTrade(Base):
    """What the screen->LLM->risk pipeline WOULD trade (shadow mode logs these
    instead of submitting). Lets the strategy be validated before going live."""
    __tablename__ = "shadow_trades"
    __table_args__ = (Index("ix_shadow_trades_date", "screen_date"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    screen_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    signal: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)  # would_buy|llm_skip|risk_reject|error
    side: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    entry: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    target: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    tier: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    confidence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    horizon: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    thesis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SanitizationLog(Base):
    __tablename__ = "sanitization_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    original_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    stripped_fragments: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sanitized_text: Mapped[str] = mapped_column(Text, nullable=False)
