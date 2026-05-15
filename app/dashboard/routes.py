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

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, not_, select

from app.core.db import SessionLocal
from app.core.redis import (
    KILL_SWITCH_KEY, is_kill_switch_active, redis_client, set_kill_switch,
)
from app.dashboard import auth as dash_auth
from app.execution import ExecutionService
from app.models import RiskEvent, Signal, Trade, TradeProposal
from app.models.enums import ProposalSide, ProposalTier, TradeSleeve, TradeStatus
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
        if rejected_only:
            stmt = stmt.where(TradeProposal.rejected_reason.is_not(None))
        rows = db.execute(stmt).scalars().all()
        items = [_row_context(p) for p in rows]
    return templates.TemplateResponse(
        "proposals_history.html",
        {
            "request": request, "proposals": items, "active": "history",
            "filters": {"tier": tier or "", "model_used": model_used or "",
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
        )
        risk = _new_risk_engine(db)
        decision = risk.validate(proposal_in, state)
        persist_decision(db, prop, decision)
        if not isinstance(decision, Approved):
            raise HTTPException(400, f"risk rejected: {decision.reason}")

    await _executor().submit_bracket(decision.proposal)


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
        "created_at": p.created_at.isoformat(timespec="seconds"),
        "age_minutes": int(age.total_seconds() // 60),
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
