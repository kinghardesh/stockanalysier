from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.risk.engine import RiskEngine
from app.risk.history import AccountState
from app.schemas import ProposalIn
from app.models.enums import ProposalSide, ProposalTier, TradeSleeve

ET = ZoneInfo("America/New_York")


@dataclass
class FakeHistory:
    round_trips: int = 0
    recent_loss: bool = False

    def round_trips_last_7d(self, ticker, as_of):
        return self.round_trips

    def had_stop_loss_within(self, ticker, since):
        return self.recent_loss


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    return db


@pytest.fixture
def whitelist():
    return ["AAPL", "MSFT", "JPM", "JNJ"]


@pytest.fixture
def sector_map():
    return {"AAPL": "tech", "MSFT": "tech", "JPM": "financials", "JNJ": "healthcare"}


@pytest.fixture
def history():
    return FakeHistory()


@pytest.fixture
def engine(fake_db, whitelist, sector_map, history):
    return RiskEngine(fake_db, whitelist, sector_map, history)


@pytest.fixture
def base_state():
    return AccountState(
        equity=Decimal("100000"),
        starting_equity_today=Decimal("100000"),
        cash=Decimal("100000"),
        positions={},
        sector_exposure={},
        now=datetime(2026, 5, 14, 10, 30, tzinfo=ET),
    )


@pytest.fixture
def base_proposal():
    return ProposalIn(
        signal_id=uuid4(),
        ticker="AAPL",
        side=ProposalSide.buy,
        entry_price=Decimal("200.00"),
        stop_price=Decimal("190.00"),
        thesis="test",
        confidence=10,
        model_used="mechanical_sma_50_200",
        tier=ProposalTier.tier_1,
        sleeve=TradeSleeve.trend,
    )
