from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Protocol

from sqlalchemy import func, select

from app.models import Trade, TradeProposal


@dataclass
class PositionSnapshot:
    ticker: str
    qty: Decimal
    avg_entry_price: Decimal


@dataclass
class AccountState:
    equity: Decimal
    starting_equity_today: Decimal
    cash: Decimal
    positions: dict[str, PositionSnapshot]
    sector_exposure: dict[str, Decimal]
    now: datetime  # tz-aware, America/New_York
    # Phase 4: $ exposure per sleeve, used by the sleeve-cap check.
    sleeve_exposure: dict[str, Decimal] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.sleeve_exposure is None:
            self.sleeve_exposure = {}


class TradeHistoryProvider(Protocol):
    def round_trips_last_7d(self, ticker: str, as_of: datetime) -> int: ...
    def had_stop_loss_within(self, ticker: str, since: datetime) -> bool: ...


class DBTradeHistory:
    def __init__(self, db):
        self.db = db

    def round_trips_last_7d(self, ticker: str, as_of: datetime) -> int:
        since = as_of - timedelta(days=7)
        stmt = (
            select(func.count(Trade.id))
            .join(TradeProposal, Trade.proposal_id == TradeProposal.id)
            .where(
                TradeProposal.ticker == ticker,
                Trade.closed_at.is_not(None),
                Trade.closed_at >= since,
            )
        )
        return int(self.db.execute(stmt).scalar_one() or 0)

    def had_stop_loss_within(self, ticker: str, since: datetime) -> bool:
        stmt = (
            select(Trade.id)
            .join(TradeProposal, Trade.proposal_id == TradeProposal.id)
            .where(
                TradeProposal.ticker == ticker,
                Trade.closed_at.is_not(None),
                Trade.closed_at >= since,
                Trade.realized_pnl < 0,
            )
            .limit(1)
        )
        return self.db.execute(stmt).first() is not None
