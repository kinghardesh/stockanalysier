"""End-of-day summary job — Phase 4.

Runs at 16:15 ET on trading days. Aggregates today's fills, P&L by model and
sleeve, proposal counts, and writes a single `daily_summary` row.

The model attribution is the question that motivates the whole pipeline:
"did Gemini propose trades that the SMA crossover wouldn't have caught, and
were they net-positive?" That answer lives in `by_model`.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select

from app.core.db import SessionLocal
from app.core.redis import redis_client
from app.models import DailySummary, Trade, TradeProposal
from app.models.enums import TradeStatus
from app.services.equity import SOD_EQUITY_KEY

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _today_et() -> date:
    return datetime.now(ET).date()


def _today_window_utc(today: date) -> tuple[datetime, datetime]:
    start_et = datetime.combine(today, datetime.min.time(), tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def build_summary(today: date | None = None) -> DailySummary:
    today = today or _today_et()
    start_utc, end_utc = _today_window_utc(today)

    sod_raw = redis_client.get(SOD_EQUITY_KEY)
    starting = Decimal(sod_raw) if sod_raw else Decimal(0)

    by_model: dict[str, Decimal] = defaultdict(Decimal)
    by_sleeve: dict[str, Decimal] = defaultdict(Decimal)
    mechanical_pnl = Decimal(0)
    llm_pnl = Decimal(0)
    total_pnl = Decimal(0)
    fill_count = 0

    with SessionLocal() as db:
        trades_today = db.execute(
            select(Trade).where(
                Trade.opened_at >= start_utc,
                Trade.opened_at < end_utc,
                Trade.status.in_([TradeStatus.filled, TradeStatus.partial]),
            )
        ).scalars().all()

        for t in trades_today:
            fill_count += 1
            pnl = t.realized_pnl or Decimal(0)
            total_pnl += pnl
            model = t.model_used or "unknown"
            sleeve = t.sleeve.value if hasattr(t.sleeve, "value") else str(t.sleeve)
            by_model[model] += pnl
            by_sleeve[sleeve] += pnl
            if model in ("mechanical_sma_50_200", "mechanical_rsi2"):
                mechanical_pnl += pnl
            elif model in ("gemini", "ollama"):
                llm_pnl += pnl

        proposals_total = db.execute(
            select(func.count(TradeProposal.id)).where(
                TradeProposal.created_at >= start_utc,
                TradeProposal.created_at < end_utc,
            )
        ).scalar_one()
        proposals_rejected = db.execute(
            select(func.count(TradeProposal.id)).where(
                TradeProposal.created_at >= start_utc,
                TradeProposal.created_at < end_utc,
                TradeProposal.rejected_reason.is_not(None),
            )
        ).scalar_one()
        proposals_executed = db.execute(
            select(func.count(func.distinct(Trade.proposal_id))).where(
                and_(
                    Trade.opened_at >= start_utc,
                    Trade.opened_at < end_utc,
                )
            )
        ).scalar_one()

        ending = starting + total_pnl

        # Upsert by trading_date (idempotent on rerun).
        existing = db.execute(
            select(DailySummary).where(DailySummary.trading_date == today)
        ).scalar_one_or_none()
        if existing is None:
            existing = DailySummary(trading_date=today, starting_equity=starting,
                                    ending_equity=ending, total_pnl=total_pnl)
            db.add(existing)
        existing.starting_equity = starting
        existing.ending_equity = ending
        existing.total_pnl = total_pnl
        existing.fill_count = fill_count
        existing.mechanical_pnl = mechanical_pnl
        existing.llm_pnl = llm_pnl
        existing.by_model = {k: str(v) for k, v in by_model.items()}
        existing.by_sleeve = {k: str(v) for k, v in by_sleeve.items()}
        existing.proposals_total = int(proposals_total)
        existing.proposals_executed = int(proposals_executed)
        existing.proposals_rejected = int(proposals_rejected)
        db.commit()
        db.refresh(existing)
        log.info(
            "EOD %s: pnl=%s fills=%d proposals=%d (exec=%d rej=%d) mech=%s llm=%s",
            today, total_pnl, fill_count, proposals_total, proposals_executed,
            proposals_rejected, mechanical_pnl, llm_pnl,
        )
        return existing


async def run_once() -> DailySummary:
    return build_summary()
