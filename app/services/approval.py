"""Risk-validate and execute an approved Tier-3 proposal.

Shared handoff used by the GPT timeout fallback approver (and available to any
non-dashboard caller). Mirrors the dashboard's manual-approve path: rebuild the
ProposalIn from the stored row, run the risk engine, and submit the bracket.
Returns (executed, reason).
"""
import logging
from decimal import Decimal
from uuid import UUID

from app.core.db import SessionLocal
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.execution import ExecutionService
from app.models import Signal, TradeProposal
from app.models.enums import TradeSleeve
from app.risk.decision import Approved
from app.risk.engine import RiskEngine
from app.risk.history import DBTradeHistory
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.account import get_account_state
from app.services.equity import StartOfDayEquityMissing
from app.services.quotes import latest_trade_price
from app.services.sleeve_map import sleeve_for_signal_source

log = logging.getLogger(__name__)

_INDEX_SYMBOLS = ["SPY", "QQQ", "IWM"]


async def approve_and_execute(proposal_id: UUID, executor: ExecutionService | None = None) -> tuple[bool, str]:
    executor = executor or ExecutionService()
    try:
        state = get_account_state()
    except StartOfDayEquityMissing:
        return False, "start-of-day equity missing"

    with SessionLocal() as db:
        prop = db.get(TradeProposal, proposal_id)
        if prop is None:
            return False, "proposal not found"
        if prop.rejected_reason:
            return False, f"already rejected: {prop.rejected_reason}"

        try:
            entry_price = latest_trade_price(prop.ticker)
        except Exception:
            entry_price = (prop.stop_price or Decimal(0)) * Decimal("1.05")

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
        risk = RiskEngine(
            db=db, whitelist=list(WHITELIST) + _INDEX_SYMBOLS,
            sector_map=SECTOR_MAP, history=DBTradeHistory(db),
        )
        decision = risk.validate(proposal_in, state)
        persist_decision(db, prop, decision)
        if not isinstance(decision, Approved):
            return False, f"risk rejected: {decision.reason}"
        sized = decision.proposal

    await executor.submit_bracket(sized)
    log.info("approve_and_execute: submitted %s %s", sized.side, sized.ticker)
    return True, "executed"
