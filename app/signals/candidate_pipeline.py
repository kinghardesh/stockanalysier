"""Phase 3: run the top screened candidates through the LLM, size the would-be
trade, and (in shadow mode) LOG it instead of executing.

mode = "shadow"  -> log only (validate the strategy before risking anything)
mode = "auto"    -> (Phase 4) execute via the normal pipeline, portfolio-capped
mode = "approve" -> (Phase 4) route to the Tier-3 approval queue

Each top-N candidate: pull live price + fundamentals context, ask the LLM
(Gemini->GPT) whether it's a buy, and if so reconcile a bracket + size it under
the per-name cap. Everything is written to shadow_trades for review.
"""
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.execution.service import _alpaca
from app.models import CompanyFundamentals, ScreenCandidate, ShadowTrade
from app.models.enums import TimeHorizon
from app.risk.sizing import reconcile_bracket, size_position
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)


def _equity() -> Decimal:
    try:
        return Decimal(str(_alpaca().get_account().equity))
    except Exception:
        log.exception("candidate_pipeline: could not read equity")
        return Decimal("100000")


def _fundamentals(db, ticker):
    f = db.get(CompanyFundamentals, ticker)
    if not f:
        return {"sector": None, "market_cap": None, "pe": None}
    return {"sector": f.sector,
            "market_cap": (f"{float(f.market_cap):,.0f}" if f.market_cap else None),
            "pe": (float(f.pe_ratio) if f.pe_ratio else None)}


def _log(rows: list[dict]):
    if not rows:
        return
    today = date.today()
    with SessionLocal() as db:
        db.execute(delete(ShadowTrade).where(ShadowTrade.screen_date == today))
        for r in rows:
            db.add(ShadowTrade(screen_date=today, **r))
        db.commit()


async def run_candidate_pipeline(orchestrator, top_n: int | None = None) -> dict:
    top_n = top_n or settings.screen_top_n
    mode = settings.screen_execution_mode
    result = {"mode": mode, "considered": 0, "llm_buy": 0, "would_buy": 0,
              "skipped": 0, "rejected": 0, "errors": 0}

    with SessionLocal() as db:
        latest = db.execute(select(ScreenCandidate.screen_date)
                            .order_by(ScreenCandidate.screen_date.desc()).limit(1)).scalar()
        if latest is None:
            log.info("candidate_pipeline: no screen candidates yet")
            return result
        cands = db.execute(
            select(ScreenCandidate).where(ScreenCandidate.screen_date == latest)
            .order_by(ScreenCandidate.rank).limit(top_n)
        ).scalars().all()
        ctxs = []
        for c in cands:
            f = _fundamentals(db, c.ticker)
            ctxs.append((c, f))

    equity = _equity()
    per_name_cap = Decimal("1") / Decimal(max(1, settings.max_open_positions))
    shadow_rows = []

    for c, fund in ctxs:
        result["considered"] += 1
        try:
            try:
                entry = latest_trade_price(c.ticker)
            except Exception:
                entry = Decimal(str(c.close)) if c.close else None
            if not entry or entry <= 0:
                shadow_rows.append(dict(ticker=c.ticker, signal=c.signal,
                                        decision="error", reason="no price"))
                result["errors"] += 1
                continue

            assessment = await orchestrator.analyze_candidate({
                "ticker": c.ticker, "signal": c.signal, "price": f"{entry:.2f}",
                "sma50": (f"{float(c.sma50):.2f}" if c.sma50 else "n/a"),
                "sma200": (f"{float(c.sma200):.2f}" if c.sma200 else "n/a"),
                "rsi": (f"{float(c.rsi):.1f}" if c.rsi else "n/a"),
                "sector": fund["sector"], "market_cap": fund["market_cap"], "pe": fund["pe"],
            })
            if assessment is None:
                shadow_rows.append(dict(ticker=c.ticker, signal=c.signal,
                                        decision="error", reason="llm unavailable"))
                result["errors"] += 1
                continue

            if not assessment.buy or assessment.confidence < settings.screen_min_confidence:
                shadow_rows.append(dict(
                    ticker=c.ticker, signal=c.signal, decision="llm_skip",
                    confidence=assessment.confidence, thesis=assessment.thesis,
                    reason=("buy=false" if not assessment.buy else "confidence below threshold")))
                result["skipped"] += 1
                continue
            result["llm_buy"] += 1

            horizon = TimeHorizon(assessment.time_horizon)
            stop, target = reconcile_bracket(
                "buy", entry,
                Decimal(str(assessment.stop_price)) if assessment.stop_price else None,
                Decimal(str(assessment.target_price)) if assessment.target_price else None,
                horizon=horizon,
            )
            qty = size_position(equity, entry, stop, max_position_pct=per_name_cap)
            if qty < 1:
                shadow_rows.append(dict(
                    ticker=c.ticker, signal=c.signal, decision="risk_reject",
                    confidence=assessment.confidence, thesis=assessment.thesis,
                    entry=entry, stop=stop, target=target, reason="computed size < 1 share"))
                result["rejected"] += 1
                continue

            shadow_rows.append(dict(
                ticker=c.ticker, signal=c.signal, decision="would_buy", side="buy",
                qty=qty, entry=entry, stop=stop, target=target,
                tier="screen", confidence=assessment.confidence,
                horizon=horizon.value, thesis=assessment.thesis,
                reason=f"~{float(per_name_cap)*100:.0f}% cap, risk-sized"))
            result["would_buy"] += 1
            # mode auto/approve execution is wired in Phase 4 (portfolio cap).
        except Exception:
            log.exception("candidate_pipeline: failed on %s", c.ticker)
            shadow_rows.append(dict(ticker=c.ticker, signal=c.signal,
                                    decision="error", reason="pipeline error"))
            result["errors"] += 1

    _log(shadow_rows)
    log.info("candidate_pipeline: %s", result)
    return result
