"""Tier 3 approval timeout — Phase 4.

Pending Tier 3 proposals expire after TIER3_APPROVAL_TIMEOUT_MINUTES (default 30).
News headlines and 8-Ks have a half-life: a 30-minute-old "Apple beats earnings"
isn't actionable. Auto-rejecting stale proposals beats accumulating a queue of
zombies that get approved blindly the next time the operator logs in.

A proposal is considered pending iff:
  - tier = tier_3
  - rejected_reason IS NULL
  - no associated trade row in (pending, partial, filled) status

Runs every 5 minutes via APScheduler.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import not_, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Trade, TradeProposal
from app.models.enums import ProposalTier, TradeStatus

log = logging.getLogger(__name__)


def expire_stale_tier3_proposals() -> int:
    """Mark stale pending Tier 3 proposals as hitl_timeout. Returns count expired."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.tier3_approval_timeout_minutes
    )

    expired = 0
    with SessionLocal() as db:
        candidates = db.execute(
            select(TradeProposal).where(
                TradeProposal.tier == ProposalTier.tier_3,
                TradeProposal.rejected_reason.is_(None),
                TradeProposal.created_at < cutoff,
                not_(
                    TradeProposal.id.in_(
                        select(Trade.proposal_id).where(
                            Trade.status.in_([
                                TradeStatus.pending, TradeStatus.partial, TradeStatus.filled,
                            ])
                        )
                    )
                ),
            )
        ).scalars().all()

        for prop in candidates:
            prop.rejected_reason = "hitl_timeout"
            expired += 1

        if expired:
            db.commit()
            log.info("expire_stale_tier3: rejected %d stale proposals (older than %dm)",
                     expired, settings.tier3_approval_timeout_minutes)
    return expired


async def run_once() -> int:
    return expire_stale_tier3_proposals()
