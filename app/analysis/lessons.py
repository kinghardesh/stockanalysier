"""Post-trade analysis and lesson memory.

On every losing exit, Gemini analyses WHY it lost and stores a specific lesson.
That lesson is then injected into future LLM prompts for the same ticker/sector
so the system doesn't repeat the same mistake.
"""
import json
import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.models import CompanyFundamentals, TradeLesson

log = logging.getLogger(__name__)


async def analyze_and_store_loss(
    *,
    ticker: str,
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    realized_pnl: Decimal,
    thesis: str,
    signal_type: str = "unknown",
) -> Optional[TradeLesson]:
    """Call Gemini to analyze a loss, then persist the lesson. Returns the row."""
    try:
        from app.llm.providers.gemini import GeminiProvider
        from app.llm.prompts import LOSS_ANALYSIS_PROMPT
    except Exception as e:
        log.warning("lessons: could not import Gemini provider: %s", e)
        return None

    # Get sector from fundamentals
    sector = "unknown"
    with SessionLocal() as db:
        f = db.get(CompanyFundamentals, ticker)
        if f and f.sector:
            sector = f.sector

    pnl_pct = float(realized_pnl / (entry_price * Decimal(qty)) * 100) if entry_price and qty else 0.0

    prompt = LOSS_ANALYSIS_PROMPT.format(
        ticker=ticker, sector=sector, signal_type=signal_type,
        entry_price=float(entry_price), exit_price=float(exit_price),
        qty=qty, realized_pnl=float(realized_pnl), pnl_pct=pnl_pct,
        thesis=thesis or "(no thesis recorded)",
    )
    schema = {
        "type": "object",
        "properties": {
            "loss_reason": {"type": "string"},
            "lesson":      {"type": "string"},
        },
        "required": ["loss_reason", "lesson"],
    }

    try:
        provider = GeminiProvider()
        raw = await provider.generate_structured(prompt, schema, timeout=30)
        loss_reason = raw.get("loss_reason", "")
        lesson      = raw.get("lesson", "")
    except Exception as e:
        log.warning("lessons: Gemini analysis failed for %s: %s", ticker, e)
        loss_reason = "LLM analysis unavailable"
        lesson      = ""

    row = TradeLesson(
        ticker=ticker.upper(), sector=sector, signal_type=signal_type,
        entry_price=entry_price, exit_price=exit_price, qty=qty,
        realized_pnl=realized_pnl,
        pnl_pct=Decimal(str(round(pnl_pct, 4))),
        entry_thesis=thesis, loss_reason=loss_reason, lesson=lesson,
        raw_context={"thesis": thesis, "sector": sector},
    )
    with SessionLocal() as db:
        db.add(row); db.commit()

    log.info("trade_lesson stored: ticker=%s pnl=%.0f reason=%.60s",
             ticker, float(realized_pnl), loss_reason)
    return row


def get_lessons_for(ticker: str, sector: Optional[str] = None, limit: int = 3) -> list[dict]:
    """Return recent lessons relevant to a ticker or sector, for LLM injection."""
    with SessionLocal() as db:
        # ticker-specific first
        rows = db.execute(
            select(TradeLesson)
            .where(TradeLesson.ticker == ticker.upper())
            .order_by(desc(TradeLesson.created_at))
            .limit(limit)
        ).scalars().all()
        # fill up with sector lessons if needed
        if sector and len(rows) < limit:
            extra = db.execute(
                select(TradeLesson)
                .where(TradeLesson.sector == sector,
                       TradeLesson.ticker != ticker.upper())
                .order_by(desc(TradeLesson.created_at))
                .limit(limit - len(rows))
            ).scalars().all()
            rows = list(rows) + list(extra)
    return [
        {"ticker": r.ticker, "pnl": float(r.realized_pnl or 0),
         "lesson": r.lesson, "reason": r.loss_reason}
        for r in rows
    ]
