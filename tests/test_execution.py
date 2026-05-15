"""Phase 4: real-execution unit tests with Alpaca mocked.

We mock TradingClient.submit_order to return a fake order, then verify:
  - a Trade row is created with the right model_used and alpaca_order_id
  - re-submitting the same proposal raises DuplicateExecution
  - missing TradeProposal raises ExecutionError
"""
import asyncio
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.execution.service import DuplicateExecution, ExecutionError, ExecutionService
from app.models import Signal, Trade, TradeProposal
from app.models.enums import (
    ProposalSide, ProposalTier, SignalSource, TradeSleeve, TradeStatus,
)
from app.schemas import SizedProposal


@pytest.fixture
def sqlite_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    # Patch every reference to SessionLocal the executor reaches for.
    monkeypatch.setattr("app.execution.service.SessionLocal", Session)
    yield Session


@pytest.fixture
def seeded_proposal(sqlite_session):
    with sqlite_session() as db:
        sig = Signal(id=uuid4(), source=SignalSource.news, ticker="AAPL",
                     signal_type="test", raw_data={})
        db.add(sig); db.flush()
        prop = TradeProposal(
            id=uuid4(), signal_id=sig.id, ticker="AAPL",
            side=ProposalSide.buy, thesis="t", confidence=8,
            tier=ProposalTier.tier_1,
        )
        db.add(prop); db.commit()
        return prop.signal_id, prop.id


def _sized(signal_id):
    return SizedProposal(
        signal_id=signal_id,
        ticker="AAPL",
        side=ProposalSide.buy,
        entry_price=Decimal("200"),
        stop_price=Decimal("190"),
        target_price=Decimal("220"),
        thesis="t",
        confidence=8,
        model_used="gemini",
        tier=ProposalTier.tier_1,
        sleeve=TradeSleeve.trend,
        qty=10,
    )


def test_submit_creates_trade_row(sqlite_session, seeded_proposal):
    signal_id, prop_id = seeded_proposal

    mock_order = MagicMock()
    mock_order.id = "alpaca-order-1"
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    svc = ExecutionService(client=mock_client)
    order_id = asyncio.run(svc.submit_bracket(_sized(signal_id)))

    assert order_id == "alpaca-order-1"
    with sqlite_session() as db:
        trade = db.execute(select(Trade)).scalar_one()
        assert trade.alpaca_order_id == "alpaca-order-1"
        assert trade.status == TradeStatus.pending
        assert trade.model_used == "gemini"
        assert trade.proposal_id == prop_id


def test_duplicate_raises(sqlite_session, seeded_proposal):
    signal_id, _ = seeded_proposal
    mock_order = MagicMock(); mock_order.id = "x"
    mock_client = MagicMock(); mock_client.submit_order.return_value = mock_order
    svc = ExecutionService(client=mock_client)

    asyncio.run(svc.submit_bracket(_sized(signal_id)))
    with pytest.raises(DuplicateExecution):
        asyncio.run(svc.submit_bracket(_sized(signal_id)))


def test_missing_proposal_raises(sqlite_session):
    mock_client = MagicMock()
    svc = ExecutionService(client=mock_client)
    sized = _sized(uuid4())  # signal_id with no matching TradeProposal
    with pytest.raises(ExecutionError):
        asyncio.run(svc.submit_bracket(sized))


def test_zero_qty_rejected(sqlite_session, seeded_proposal):
    signal_id, _ = seeded_proposal
    sized = _sized(signal_id)
    sized = sized.model_copy(update={"qty": 0})
    svc = ExecutionService(client=MagicMock())
    with pytest.raises(ExecutionError):
        asyncio.run(svc.submit_bracket(sized))
