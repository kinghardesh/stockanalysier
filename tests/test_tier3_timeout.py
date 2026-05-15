"""Phase 4: tier-3 approval timeout regression test.

We avoid real Postgres by patching SessionLocal to a sqlite engine with the
same models. The test focuses on the SQL/business rule, not the persistence
backend — it's the rule we care about.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import Signal, Trade, TradeProposal
from app.models.enums import (
    ProposalSide, ProposalTier, SignalSource, TradeSleeve, TradeStatus,
)


@pytest.fixture
def sqlite_session(monkeypatch):
    """In-memory sqlite; we model the rows we need.

    Postgres JSONB and ENUM types degrade to TEXT here, which is fine for the
    timeout query that doesn't touch either.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.scheduler.expire_stale.SessionLocal", Session)
    yield Session


def _make_proposal(session, *, age_minutes: int, tier=ProposalTier.tier_3,
                   rejected_reason=None, with_pending_trade=False):
    signal = Signal(
        id=uuid4(),
        source=SignalSource.news,
        ticker="AAPL",
        signal_type="test",
        raw_data={},
    )
    session.add(signal); session.flush()

    proposal = TradeProposal(
        id=uuid4(),
        signal_id=signal.id,
        ticker="AAPL",
        side=ProposalSide.buy,
        thesis="t",
        confidence=8,
        tier=tier,
        rejected_reason=rejected_reason,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    session.add(proposal); session.flush()

    if with_pending_trade:
        trade = Trade(
            id=uuid4(),
            proposal_id=proposal.id,
            status=TradeStatus.pending,
            sleeve=TradeSleeve.trend,
        )
        session.add(trade); session.flush()

    session.commit()
    return proposal


def test_fresh_tier3_not_expired(sqlite_session):
    with sqlite_session() as db:
        _make_proposal(db, age_minutes=5)

    with patch("app.scheduler.expire_stale.settings") as mock_settings:
        mock_settings.tier3_approval_timeout_minutes = 30
        from app.scheduler.expire_stale import expire_stale_tier3_proposals
        assert expire_stale_tier3_proposals() == 0


def test_stale_tier3_gets_rejected(sqlite_session):
    with sqlite_session() as db:
        prop = _make_proposal(db, age_minutes=45)
        prop_id = prop.id

    with patch("app.scheduler.expire_stale.settings") as mock_settings:
        mock_settings.tier3_approval_timeout_minutes = 30
        from app.scheduler.expire_stale import expire_stale_tier3_proposals
        assert expire_stale_tier3_proposals() == 1

    with sqlite_session() as db:
        row = db.get(TradeProposal, prop_id)
        assert row.rejected_reason == "hitl_timeout"


def test_already_rejected_not_touched(sqlite_session):
    with sqlite_session() as db:
        _make_proposal(db, age_minutes=120, rejected_reason="bear_case_invalidated")

    with patch("app.scheduler.expire_stale.settings") as mock_settings:
        mock_settings.tier3_approval_timeout_minutes = 30
        from app.scheduler.expire_stale import expire_stale_tier3_proposals
        assert expire_stale_tier3_proposals() == 0


def test_tier3_with_pending_trade_not_expired(sqlite_session):
    """If a Trade row exists in pending/partial/filled, the proposal was
    approved — don't double-reject it."""
    with sqlite_session() as db:
        _make_proposal(db, age_minutes=60, with_pending_trade=True)

    with patch("app.scheduler.expire_stale.settings") as mock_settings:
        mock_settings.tier3_approval_timeout_minutes = 30
        from app.scheduler.expire_stale import expire_stale_tier3_proposals
        assert expire_stale_tier3_proposals() == 0


def test_tier1_never_expired(sqlite_session):
    with sqlite_session() as db:
        _make_proposal(db, age_minutes=240, tier=ProposalTier.tier_1)

    with patch("app.scheduler.expire_stale.settings") as mock_settings:
        mock_settings.tier3_approval_timeout_minutes = 30
        from app.scheduler.expire_stale import expire_stale_tier3_proposals
        assert expire_stale_tier3_proposals() == 0
