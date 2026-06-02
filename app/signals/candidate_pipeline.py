"""Phase 3/4: run the top screened candidates through the LLM, size the would-be
trade, and act on it according to the execution mode.

mode = "shadow"  -> log only (validate the strategy before risking anything)
mode = "auto"    -> execute via a bracket, capped at max_open_positions, skipping
                    names already held
mode = "approve" -> create a Tier-3 proposal that lands in the approval queue

Every candidate's outcome is written to shadow_trades for review regardless of
mode (decision = would_buy | executed | queued | portfolio_full | already_held |
llm_skip | risk_reject | error).
"""
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.execution import ExecutionService
from app.execution.service import _alpaca
from app.models import (
    CompanyFundamentals, ScreenCandidate, ShadowTrade, Signal, TradeProposal,
)
from app.llm.schemas import CandidateAssessment
from app.models.enums import (
    ProposalSide, ProposalTier, SignalSource, TimeHorizon, TradeSleeve,
)
from app.risk.sizing import reconcile_bracket, size_position
from app.schemas import SizedProposal
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)


def _equity() -> Decimal:
    try:
        return Decimal(str(_alpaca().get_account().equity))
    except Exception:
        log.exception("candidate_pipeline: could not read equity")
        return Decimal("100000")


def _cap_state():
    """(set of held symbols, open position count) — the portfolio cap is on the
    whole account, not just screen-driven positions."""
    try:
        positions = _alpaca().get_all_positions()
        return {p.symbol.upper() for p in positions}, len(positions)
    except Exception:
        log.exception("candidate_pipeline: could not list positions")
        return set(), 0


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


async def _execute_or_queue(order: dict, mode: str, executor: ExecutionService,
                            per_name_cap: Decimal) -> tuple[str, str]:
    """Create the signal + proposal rows; submit a bracket (auto) or leave it
    Tier-3 for approval (approve). Returns (decision, reason)."""
    momentum = order["signal"] == "momentum"
    source = SignalSource.sma_crossover if momentum else SignalSource.rsi_mean_reversion
    sleeve = TradeSleeve.trend if momentum else TradeSleeve.mean_reversion
    tier = ProposalTier.tier_2 if mode == "auto" else ProposalTier.tier_3
    horizon = order["horizon"]
    try:
        with SessionLocal() as db:
            sig = Signal(
                source=source, ticker=order["ticker"],
                signal_type=f"screen_{order['signal']}",
                raw_data={"screen": True, "thesis": order["thesis"],
                          "confidence": order["confidence"],
                          "time_horizon": horizon.value, "model_used": "screen_llm"},
            )
            db.add(sig); db.flush()
            db.add(TradeProposal(
                signal_id=sig.id, ticker=order["ticker"], side=ProposalSide.buy,
                proposed_size_pct=per_name_cap, stop_price=order["stop"],
                target_price=order["target"], thesis=order["thesis"],
                confidence=order["confidence"], model_used="screen_llm",
                tier=tier, time_horizon=horizon,
            ))
            db.commit()
            sig_id = sig.id
    except Exception as e:
        log.exception("candidate_pipeline: could not persist proposal for %s", order["ticker"])
        return "error", f"persist failed: {str(e)[:80]}"

    if mode == "approve":
        return "queued", "tier_3 approval queue"

    sized = SizedProposal(
        signal_id=sig_id, ticker=order["ticker"], side=ProposalSide.buy,
        entry_price=order["entry"], stop_price=order["stop"], target_price=order["target"],
        thesis=order["thesis"], confidence=order["confidence"], model_used="screen_llm",
        tier=tier, sleeve=sleeve, time_horizon=horizon, proposed_size_pct=per_name_cap,
        qty=order["qty"],
    )
    try:
        await executor.submit_bracket(sized)
        return "executed", "submitted bracket"
    except Exception as e:
        log.exception("candidate_pipeline: execute failed for %s", order["ticker"])
        return "error", f"execute failed: {str(e)[:80]}"


async def run_candidate_pipeline(orchestrator, executor: ExecutionService | None = None,
                                 top_n: int | None = None) -> dict:
    top_n = top_n or settings.screen_top_n
    mode = settings.screen_execution_mode
    executor = executor or ExecutionService()
    result = {"mode": mode, "considered": 0, "llm_buy": 0, "would_buy": 0,
              "executed": 0, "queued": 0, "already_held": 0, "portfolio_full": 0,
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
        ctxs = [(c, _fundamentals(db, c.ticker)) for c in cands]

    equity = _equity()
    per_name_cap = Decimal("1") / Decimal(max(1, settings.max_open_positions))
    held, open_count = (set(), 0)
    if mode != "shadow":
        held, open_count = _cap_state()
    room = settings.max_open_positions - open_count
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

            if settings.screen_llm_vet:
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
            else:
                # Mechanical-only: take the screen signal as the buy (no LLM gate).
                assessment = CandidateAssessment(
                    buy=True, confidence=settings.screen_min_confidence,
                    thesis=f"mechanical {c.signal} signal (score {float(c.score):.1f})",
                    stop_price=float(c.suggested_stop) if c.suggested_stop else None,
                    target_price=None,
                    time_horizon=("position" if c.signal == "momentum" else "swing"),
                )

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

            order = dict(ticker=c.ticker, signal=c.signal, entry=entry, stop=stop,
                         target=target, qty=qty, confidence=assessment.confidence,
                         horizon=horizon, thesis=assessment.thesis)

            if mode == "shadow":
                decision, reason = "would_buy", f"~{float(per_name_cap)*100:.0f}% cap, risk-sized"
            elif c.ticker.upper() in held:
                decision, reason = "already_held", "position already open"
            elif room <= 0:
                decision, reason = "portfolio_full", f"at {settings.max_open_positions}-position cap"
            else:
                decision, reason = await _execute_or_queue(order, mode, executor, per_name_cap)
                if decision == "executed":
                    room -= 1
                    held.add(c.ticker.upper())

            shadow_rows.append(dict(
                ticker=c.ticker, signal=c.signal, decision=decision, side="buy",
                qty=qty, entry=entry, stop=stop, target=target, tier="screen",
                confidence=assessment.confidence, horizon=horizon.value,
                thesis=assessment.thesis, reason=reason))
            result[decision] = result.get(decision, 0) + 1
        except Exception:
            log.exception("candidate_pipeline: failed on %s", c.ticker)
            shadow_rows.append(dict(ticker=c.ticker, signal=c.signal,
                                    decision="error", reason="pipeline error"))
            result["errors"] += 1

    _log(shadow_rows)
    log.info("candidate_pipeline: %s", result)
    return result
