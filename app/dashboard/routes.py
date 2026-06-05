"""HTMX-driven dashboard routes — Phase 4.

Pages:
  GET  /dashboard                       redirect to pending
  GET  /dashboard/login                 token form
  POST /dashboard/login                 sets cookie on match
  GET  /dashboard/logout                clears cookie
  GET  /dashboard/proposals/pending     Tier 3 cards awaiting approval
  GET  /dashboard/proposals/history     paginated table (filters: tier, model, rejected_reason)
  GET  /dashboard/risk-events           recent risk_events log
  GET  /dashboard/system                kill switch, SOD equity, rate limit counters

HTMX actions:
  POST /dashboard/proposals/{id}/approve     submit to executor, swap card out
  POST /dashboard/proposals/{id}/reject      mark rejected, swap card out
  POST /dashboard/system/kill-switch         toggle, return refreshed system panel
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, not_, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import (
    KILL_SWITCH_KEY, is_kill_switch_active, redis_client, set_kill_switch,
)
from app.dashboard import auth as dash_auth
from app.execution import ExecutionService
from app.models import (
    RiskEvent, ScreenCandidate, ShadowTrade, Signal, Trade, TradeProposal,
)
from app.models.enums import (
    ProposalSide, ProposalTier, TimeHorizon, TradeSleeve, TradeStatus, horizon_bucket,
)
from app.services.sleeve_map import sleeve_for_signal_source
from app.risk.engine import RiskEngine
from app.risk.history import DBTradeHistory
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.risk.decision import Approved
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.account import get_account_state
from app.services.equity import SOD_EQUITY_KEY, StartOfDayEquityMissing
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

ET = ZoneInfo("America/New_York")


def _executor() -> ExecutionService:
    return ExecutionService()


def _new_risk_engine(db) -> RiskEngine:
    return RiskEngine(
        db=db, whitelist=WHITELIST, sector_map=SECTOR_MAP, history=DBTradeHistory(db),
    )


# --- auth pages -----------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse(url="/dashboard/proposals/pending",
                            status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, token: str = Form(...)):
    if not dash_auth.verify_token(token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid token."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    resp = RedirectResponse(url="/dashboard/proposals/pending",
                            status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=dash_auth.COOKIE_NAME,
        value=dash_auth.issue_cookie_value(),
        max_age=dash_auth.COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(url="/dashboard/login",
                            status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(dash_auth.COOKIE_NAME)
    return resp


# --- pages ---------------------------------------------------------------

@router.get("/proposals/pending", response_class=HTMLResponse)
def pending(request: Request, _=Depends(dash_auth.require_auth)):
    with SessionLocal() as db:
        proposals = db.execute(
            select(TradeProposal).where(
                TradeProposal.tier == ProposalTier.tier_3,
                TradeProposal.rejected_reason.is_(None),
                not_(
                    TradeProposal.id.in_(
                        select(Trade.proposal_id).where(
                            Trade.status.in_([
                                TradeStatus.pending, TradeStatus.partial, TradeStatus.filled,
                            ])
                        )
                    )
                ),
            ).order_by(desc(TradeProposal.created_at))
        ).scalars().all()
        items = [_card_context(p) for p in proposals]
    return templates.TemplateResponse(
        "proposals_pending.html",
        {"request": request, "proposals": items, "active": "pending"},
    )


@router.get("/proposals/history", response_class=HTMLResponse)
def history(
    request: Request,
    tier: Optional[str] = Query(None),
    model_used: Optional[str] = Query(None),
    horizon: Optional[str] = Query(None),
    rejected_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    _=Depends(dash_auth.require_auth),
):
    with SessionLocal() as db:
        stmt = select(TradeProposal).order_by(desc(TradeProposal.created_at)).limit(limit)
        if tier in {"tier_1", "tier_2", "tier_3"}:
            stmt = stmt.where(TradeProposal.tier == ProposalTier(tier))
        if model_used:
            stmt = stmt.where(TradeProposal.model_used == model_used)
        if horizon == "short_term":
            stmt = stmt.where(TradeProposal.time_horizon.in_(
                [TimeHorizon.intraday, TimeHorizon.swing]))
        elif horizon == "long_term":
            stmt = stmt.where(TradeProposal.time_horizon == TimeHorizon.position)
        if rejected_only:
            stmt = stmt.where(TradeProposal.rejected_reason.is_not(None))
        rows = db.execute(stmt).scalars().all()
        items = [_row_context(p) for p in rows]
    return templates.TemplateResponse(
        "proposals_history.html",
        {
            "request": request, "proposals": items, "active": "history",
            "filters": {"tier": tier or "", "model_used": model_used or "",
                        "horizon": horizon or "",
                        "rejected_only": rejected_only, "limit": limit},
        },
    )


@router.get("/risk-events", response_class=HTMLResponse)
def risk_events(request: Request, limit: int = Query(100, ge=1, le=500),
                _=Depends(dash_auth.require_auth)):
    with SessionLocal() as db:
        events = db.execute(
            select(RiskEvent).order_by(desc(RiskEvent.timestamp)).limit(limit)
        ).scalars().all()
        items = [{
            "id": str(e.id),
            "timestamp": e.timestamp.isoformat(timespec="seconds"),
            "event_type": e.event_type.value,
            "reason": e.reason,
            "related_proposal_id": str(e.related_proposal_id) if e.related_proposal_id else None,
        } for e in events]
    return templates.TemplateResponse(
        "risk_events.html",
        {"request": request, "events": items, "active": "risk"},
    )


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, _=Depends(dash_auth.require_auth)):
    """Read-only view of live Alpaca positions with horizon + time-to-exit."""
    error = None
    items: list[dict] = []
    try:
        live = _executor().list_positions()
    except Exception:
        log.exception("positions: could not list Alpaca positions")
        live, error = [], "Could not reach Alpaca to list live positions."
    if live:
        with SessionLocal() as db:
            items = [_position_context(db, pos) for pos in live]
        items.sort(key=lambda r: (r["bucket"] != "short_term", r["ticker"]))
    return templates.TemplateResponse(
        "positions.html",
        {
            "request": request, "positions": items, "active": "positions", "error": error,
            "swing_max_hold_days": settings.swing_max_hold_days,
            "intraday_eod_close_et": settings.intraday_eod_close_et,
        },
    )


@router.get("/lessons", response_class=HTMLResponse)
def lessons_page(request: Request, _=Depends(dash_auth.require_auth)):
    from app.models import TradeLesson
    with SessionLocal() as db:
        rows = db.execute(
            select(TradeLesson).order_by(desc(TradeLesson.created_at)).limit(50)
        ).scalars().all()
    items = [{
        "ticker": r.ticker, "sector": r.sector or "",
        "signal_type": r.signal_type or "",
        "entry_price": float(r.entry_price) if r.entry_price else None,
        "exit_price": float(r.exit_price) if r.exit_price else None,
        "pnl": float(r.realized_pnl or 0),
        "pnl_pct": float(r.pnl_pct or 0),
        "loss_reason": r.loss_reason or "",
        "lesson": r.lesson or "",
        "date": r.created_at.strftime("%Y-%m-%d") if r.created_at else "",
    } for r in rows]
    return templates.TemplateResponse(
        "lessons.html", {"request": request, "active": "lessons", "lessons": items})


@router.get("/screen", response_class=HTMLResponse)
def screen(request: Request, _=Depends(dash_auth.require_auth)):
    """Daily mechanical screen candidates + the shadow (would-be) trades."""
    with SessionLocal() as db:
        cand_date = db.execute(select(func.max(ScreenCandidate.screen_date))).scalar()
        candidates = []
        if cand_date is not None:
            candidates = db.execute(
                select(ScreenCandidate).where(ScreenCandidate.screen_date == cand_date)
                .order_by(ScreenCandidate.rank).limit(50)
            ).scalars().all()
        shadow_date = db.execute(select(func.max(ShadowTrade.screen_date))).scalar()
        shadows = []
        if shadow_date is not None:
            shadows = db.execute(
                select(ShadowTrade).where(ShadowTrade.screen_date == shadow_date)
                .order_by(ShadowTrade.decision, ShadowTrade.ticker)
            ).scalars().all()
        cand_items = [_candidate_ctx(c) for c in candidates]
        shadow_items = [_shadow_ctx(s) for s in shadows]
    return templates.TemplateResponse(
        "screen.html",
        {
            "request": request, "active": "screen",
            "candidates": cand_items, "shadows": shadow_items,
            "cand_date": str(cand_date) if cand_date else "—",
            "shadow_date": str(shadow_date) if shadow_date else "—",
            "exec_mode": settings.screen_execution_mode,
            "would_buy": sum(1 for s in shadow_items if s["decision"] == "would_buy"),
        },
    )


@router.get("/system", response_class=HTMLResponse)
def system_panel(request: Request, _=Depends(dash_auth.require_auth)):
    return templates.TemplateResponse(
        "system.html",
        {"request": request, "active": "system", **_system_context()},
    )


# --- HTMX actions --------------------------------------------------------

@router.post("/proposals/{proposal_id}/approve", response_class=HTMLResponse)
async def approve(proposal_id: UUID, request: Request, _=Depends(dash_auth.require_auth)):
    with SessionLocal() as db:
        prop = db.get(TradeProposal, proposal_id)
        if prop is None:
            raise HTTPException(404, "proposal not found")
        if prop.rejected_reason:
            raise HTTPException(409, f"already rejected: {prop.rejected_reason}")
        existing_trade = db.execute(
            select(Trade).where(
                Trade.proposal_id == prop.id,
                Trade.status.in_([TradeStatus.pending, TradeStatus.partial, TradeStatus.filled]),
            )
        ).scalar_one_or_none()
        if existing_trade is not None:
            raise HTTPException(409, "already executing")

    # Run through risk + execution. Heavy lifting in its own session.
    try:
        await _execute_approved(proposal_id)
    except StartOfDayEquityMissing:
        raise HTTPException(503, "SOD equity missing; kill switch engaged")
    except Exception:
        log.exception("approve failed for %s", proposal_id)
        raise HTTPException(500, "execution failed; check worker logs")

    # HTMX: returning empty body with HX-Trigger removes the card cleanly.
    return HTMLResponse(
        content=f'<div class="text-xs text-slate-400 italic">approved &amp; submitted ({proposal_id})</div>',
        headers={"HX-Trigger": "proposal-approved"},
    )


@router.post("/proposals/{proposal_id}/reject", response_class=HTMLResponse)
async def reject(proposal_id: UUID, request: Request,
                 reason: str = Form("hitl_rejected"),
                 _=Depends(dash_auth.require_auth)):
    with SessionLocal() as db:
        prop = db.get(TradeProposal, proposal_id)
        if prop is None:
            raise HTTPException(404, "proposal not found")
        if prop.rejected_reason:
            return HTMLResponse(
                content=f'<div class="text-xs text-slate-400 italic">already rejected: {prop.rejected_reason}</div>',
            )
        prop.rejected_reason = reason
        db.commit()
    return HTMLResponse(
        content=f'<div class="text-xs text-slate-400 italic">rejected ({reason})</div>',
        headers={"HX-Trigger": "proposal-rejected"},
    )


@router.post("/system/kill-switch", response_class=HTMLResponse)
def toggle_kill_switch(request: Request, _=Depends(dash_auth.require_auth)):
    if is_kill_switch_active():
        set_kill_switch(False)
    else:
        set_kill_switch(True)
    return templates.TemplateResponse(
        "_system_panel.html",
        {"request": request, **_system_context()},
    )


# --- helpers -------------------------------------------------------------

async def _execute_approved(proposal_id: UUID) -> None:
    """Wraps the risk-engine + executor handoff for an approved Tier 3 proposal."""
    state = get_account_state()
    with SessionLocal() as db:
        prop = db.get(TradeProposal, proposal_id)
        if prop is None:
            raise HTTPException(404, "proposal not found")

        try:
            entry_price = latest_trade_price(prop.ticker)
        except Exception:
            entry_price = (prop.stop_price or 0) * 1.05

        # Derive sleeve from the originating signal source rather than hardcoding.
        sig = db.get(Signal, prop.signal_id)
        sleeve = sleeve_for_signal_source(sig.source) if sig else TradeSleeve.discretionary

        proposal_in = ProposalIn(
            signal_id=prop.signal_id,
            ticker=prop.ticker,
            side=prop.side,
            entry_price=entry_price,
            stop_price=prop.stop_price,
            target_price=prop.target_price,
            thesis=prop.thesis,
            confidence=prop.confidence,
            model_used=prop.model_used,
            tier=prop.tier,
            sleeve=sleeve,
            time_horizon=prop.time_horizon,
            proposed_size_pct=prop.proposed_size_pct,
        )
        risk = _new_risk_engine(db)
        decision = risk.validate(proposal_in, state)
        persist_decision(db, prop, decision)
        if not isinstance(decision, Approved):
            raise HTTPException(400, f"risk rejected: {decision.reason}")

    await _executor().submit_bracket(decision.proposal)


def _candidate_ctx(c: ScreenCandidate) -> dict:
    return {
        "rank": c.rank, "ticker": c.ticker, "signal": c.signal,
        "score": float(c.score) if c.score is not None else None,
        "close": float(c.close) if c.close else None,
        "rsi": float(c.rsi) if c.rsi is not None else None,
        "sma50": float(c.sma50) if c.sma50 else None,
        "sma200": float(c.sma200) if c.sma200 else None,
    }


def _shadow_ctx(s: ShadowTrade) -> dict:
    entry = float(s.entry) if s.entry else None
    stop = float(s.stop) if s.stop else None
    target = float(s.target) if s.target else None
    rr = None
    if entry and stop and target and entry != stop:
        rr = round((target - entry) / (entry - stop), 2)
    return {
        "ticker": s.ticker, "signal": s.signal, "decision": s.decision,
        "side": s.side, "qty": s.qty, "entry": entry, "stop": stop, "target": target,
        "rr": rr, "tier": s.tier, "confidence": s.confidence, "horizon": s.horizon,
        "thesis": s.thesis, "reason": s.reason,
    }


def _verifier_info(p: TradeProposal) -> dict:
    """Pull the GPT tie-breaker verdict off the originating signal (best-effort).

    Stored in Signal.raw_data; accessed lazily within the request's session.
    """
    try:
        raw = (p.signal.raw_data or {}) if p.signal else {}
    except Exception:
        raw = {}
    return {
        "verifier": raw.get("verifier_verdict") or "",
        "verifier_model": raw.get("verifier_model") or "",
        "verifier_conf": raw.get("verifier_confidence"),
        "verifier_reason": raw.get("verifier_reasoning") or "",
    }


def _card_context(p: TradeProposal) -> dict:
    age = datetime.now(timezone.utc) - p.created_at
    return {
        "id": str(p.id),
        "ticker": p.ticker,
        "side": p.side.value,
        "size_pct": float(p.proposed_size_pct or 0),
        "stop": float(p.stop_price) if p.stop_price else None,
        "target": float(p.target_price) if p.target_price else None,
        "thesis": p.thesis,
        "confidence": p.confidence,
        "tier": p.tier.value,
        "model_used": p.model_used,
        "horizon": p.time_horizon.value if p.time_horizon else "",
        "bucket": horizon_bucket(p.time_horizon),
        "created_at": p.created_at.isoformat(timespec="seconds"),
        "age_minutes": int(age.total_seconds() // 60),
        **_verifier_info(p),
    }


def _row_context(p: TradeProposal) -> dict:
    return {
        "id": str(p.id),
        "ticker": p.ticker,
        "side": p.side.value,
        "tier": p.tier.value,
        "model_used": p.model_used or "",
        "confidence": p.confidence,
        "rejected_reason": p.rejected_reason or "",
        "created_at": p.created_at.isoformat(timespec="seconds"),
        "stop": float(p.stop_price) if p.stop_price else None,
        "target": float(p.target_price) if p.target_price else None,
        "horizon": p.time_horizon.value if p.time_horizon else "",
        "bucket": horizon_bucket(p.time_horizon),
        **_verifier_info(p),
    }


def _fmt_duration(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _position_context(db, pos: dict) -> dict:
    """Build a display row for one live Alpaca position.

    Horizon + entry time come from the most recent live pipeline trade for the
    ticker; time-to-exit is derived from the configured horizon thresholds.
    """
    ticker = (pos.get("ticker") or "").upper()
    row = db.execute(
        select(Trade, TradeProposal)
        .join(TradeProposal, Trade.proposal_id == TradeProposal.id)
        .where(
            TradeProposal.ticker == ticker,
            Trade.status.in_([TradeStatus.filled, TradeStatus.partial]),
        )
        .order_by(desc(Trade.opened_at))
        .limit(1)
    ).first()

    horizon = row[1].time_horizon if row else None
    opened_at = row[0].opened_at if row else None

    time_held = "—"
    if opened_at is not None:
        time_held = _fmt_duration(datetime.now(timezone.utc) - opened_at)

    if horizon == TimeHorizon.intraday:
        time_to_exit = f"by {settings.intraday_eod_close_et} ET today"
    elif horizon == TimeHorizon.swing and opened_at is not None:
        days_held = (datetime.now(ET).date() - opened_at.astimezone(ET).date()).days
        days_left = settings.swing_max_hold_days - days_held
        time_to_exit = (
            "due now (time stop)" if days_left <= 0
            else f"~{days_left}d left (max {settings.swing_max_hold_days}d)"
        )
    elif horizon == TimeHorizon.position:
        time_to_exit = "stop / target only"
    else:
        time_to_exit = "—"

    return {
        "ticker": ticker,
        "qty": float(pos.get("qty") or 0),
        "avg_entry_price": float(pos.get("avg_entry_price") or 0),
        "unrealized_pl": float(pos.get("unrealized_pl") or 0),
        "side": pos.get("side") or "",
        "horizon": horizon.value if horizon else "",
        "bucket": horizon_bucket(horizon),
        "opened_at": opened_at.isoformat(timespec="seconds") if opened_at else None,
        "time_held": time_held,
        "time_to_exit": time_to_exit,
    }


def _system_context() -> dict:
    sod = redis_client.get(SOD_EQUITY_KEY)
    kill_ttl = redis_client.ttl(KILL_SWITCH_KEY) if is_kill_switch_active() else None

    # Rate limit counters (best-effort — keys may not exist yet today).
    now_minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    now_day = datetime.now(timezone.utc).strftime("%Y%m%d")
    rate_counters = {
        "gemini": {
            "minute": int(redis_client.get(f"ratelimit:gemini:minute:{now_minute}") or 0),
            "day": int(redis_client.get(f"ratelimit:gemini:day:{now_day}") or 0),
        },
        "ollama": {
            "minute": int(redis_client.get(f"ratelimit:ollama:minute:{now_minute}") or 0),
            "day": int(redis_client.get(f"ratelimit:ollama:day:{now_day}") or 0),
        },
    }
    return {
        "kill_switch": is_kill_switch_active(),
        "kill_ttl": kill_ttl,
        "sod_equity": sod or "(not set)",
        "rate_counters": rate_counters,
    }
