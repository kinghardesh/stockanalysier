"""Real Alpaca paper execution.

Replaces the Phase 2/3 stub. Submits bracket orders (entry + stop + take-profit)
and writes a Trade row in 'pending' state. Order updates land on the TradingStream
consumer (app/execution/order_stream.py) which transitions the row to filled / partial /
cancelled / rejected.
"""
import logging
from decimal import Decimal
from typing import Optional
from uuid import UUID

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest,
    StopLossRequest, TakeProfitRequest,
)
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Trade, TradeProposal
from app.models.enums import ProposalSide, TradeStatus
from app.schemas import SizedProposal

log = logging.getLogger(__name__)


class ExecutionError(Exception):
    pass


class DuplicateExecution(ExecutionError):
    """Refusing to submit a second order for a proposal that's already pending/filled."""


def _alpaca() -> TradingClient:
    if not settings.alpaca_paper:
        raise RuntimeError("Refusing to instantiate live TradingClient; paper only.")
    return TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)


class ExecutionService:
    """Submits bracket orders to Alpaca paper and writes a pending Trade row.

    The submit_bracket() method is idempotent at the proposal level: if a Trade
    row already exists with status in (pending, filled, partial) for the proposal,
    the call raises DuplicateExecution rather than placing a second order.
    """

    def __init__(self, client: Optional[TradingClient] = None):
        self._client = client  # lazy if None

    def _trading(self) -> TradingClient:
        if self._client is None:
            self._client = _alpaca()
        return self._client

    async def submit_bracket(self, proposal: SizedProposal) -> str:
        if proposal.qty <= 0:
            raise ExecutionError(f"qty {proposal.qty} must be > 0")
        if proposal.stop_price is None or proposal.stop_price <= 0:
            raise ExecutionError("stop_price required for bracket order")

        with SessionLocal() as db:
            # Resolve the live TradeProposal first — SizedProposal carries signal_id,
            # not the proposal row's primary key. Trade.proposal_id references
            # TradeProposal.id, so we look that up before checking for duplicates.
            proposal_row = db.execute(
                select(TradeProposal).where(
                    TradeProposal.signal_id == proposal.signal_id,
                    TradeProposal.rejected_reason.is_(None),
                ).order_by(TradeProposal.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            if proposal_row is None:
                raise ExecutionError(
                    f"no live (non-rejected) TradeProposal for signal {proposal.signal_id}"
                )

            existing = db.execute(
                select(Trade).where(
                    Trade.proposal_id == proposal_row.id,
                    Trade.status.in_([TradeStatus.pending, TradeStatus.filled, TradeStatus.partial]),
                )
            ).scalar_one_or_none()
            if existing:
                raise DuplicateExecution(
                    f"Trade {existing.id} already exists for proposal {proposal_row.id} "
                    f"in status {existing.status}"
                )

            order = self._submit(proposal)
            trade = Trade(
                proposal_id=proposal_row.id,
                alpaca_order_id=str(order.id),
                status=TradeStatus.pending,
                filled_qty=None,
                filled_price=None,
                sleeve=proposal.sleeve,
                model_used=proposal.model_used,
            )
            db.add(trade)
            db.commit()
            log.info(
                "submitted bracket %s %s qty=%d entry=%s stop=%s tp=%s alpaca_order=%s",
                proposal.side, proposal.ticker, proposal.qty,
                proposal.entry_price, proposal.stop_price, proposal.target_price, order.id,
            )
            return str(order.id)

    def _submit(self, proposal: SizedProposal):
        client = self._trading()
        side = OrderSide.BUY if proposal.side == ProposalSide.buy else OrderSide.SELL

        stop_loss = StopLossRequest(stop_price=float(proposal.stop_price))
        take_profit = (
            TakeProfitRequest(limit_price=float(proposal.target_price))
            if proposal.target_price is not None else None
        )

        if proposal.target_price is not None:
            # Bracket order requires both legs. Use a limit entry near current price
            # (the LLM/mechanical engine already chose entry_price as a reasonable level).
            req = LimitOrderRequest(
                symbol=proposal.ticker,
                qty=proposal.qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=float(proposal.entry_price),
                order_class=OrderClass.BRACKET,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        else:
            # OTO (one-triggers-other) — market entry with a trailing stop only.
            # Alpaca requires the protective leg even without a target.
            req = MarketOrderRequest(
                symbol=proposal.ticker,
                qty=proposal.qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.OTO,
                stop_loss=stop_loss,
            )
        return client.submit_order(req)

    async def submit(self, proposal: SizedProposal) -> str:
        """Backwards-compatible alias for the Phase 2/3 stub callers."""
        return await self.submit_bracket(proposal)

    async def cancel(self, alpaca_order_id: str) -> None:
        client = self._trading()
        try:
            client.cancel_order_by_id(alpaca_order_id)
        except Exception:
            log.exception("cancel failed for %s", alpaca_order_id)
            raise

    async def close_position(self, ticker: str) -> Optional[str]:
        """Close an open position at market. Returns Alpaca order id."""
        client = self._trading()
        try:
            order = client.close_position(symbol_or_asset_id=ticker)
            return str(order.id)
        except Exception:
            log.exception("close_position failed for %s", ticker)
            return None
