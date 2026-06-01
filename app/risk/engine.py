import json
import logging
from datetime import time, timedelta
from decimal import Decimal

from app.core.redis import KILL_SWITCH_KEY, redis_client
from app.models import RiskEvent, RiskEventType
from app.risk.decision import Approved, Rejected, RiskDecision
from app.risk.history import AccountState, TradeHistoryProvider
from app.risk.sizing import MAX_SINGLE_TICKER_PCT, reconcile_bracket, size_position
from app.risk.sleeve_caps import proposed_sleeve_breach
from app.schemas import ProposalIn, SizedProposal

log = logging.getLogger(__name__)

DAILY_LOSS_LIMIT = Decimal("0.03")
MAX_SECTOR_PCT = Decimal("0.40")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
ROUND_TRIP_LIMIT = 5
REENTRY_COOLDOWN_HOURS = 2
KILL_SWITCH_TTL_SECONDS = 24 * 60 * 60


class RiskEngine:
    def __init__(
        self,
        db,
        whitelist: list[str],
        sector_map: dict[str, str],
        history: TradeHistoryProvider,
    ):
        self.db = db
        self.whitelist = set(whitelist)
        self.sector_map = sector_map
        self.history = history

    def validate(self, proposal: ProposalIn, state: AccountState) -> RiskDecision:
        if proposal.ticker not in self.whitelist:
            return self._reject(proposal, state, "ticker not in whitelist")

        nt = state.now.timetz().replace(tzinfo=None)
        if not (MARKET_OPEN <= nt <= MARKET_CLOSE):
            return self._reject(proposal, state, "outside 9:30-16:00 ET trading window")

        if state.starting_equity_today > 0:
            loss_pct = (state.starting_equity_today - state.equity) / state.starting_equity_today
            if loss_pct >= DAILY_LOSS_LIMIT:
                redis_client.set(KILL_SWITCH_KEY, "true", ex=KILL_SWITCH_TTL_SECONDS)
                self._log_event(
                    proposal, state, RiskEventType.circuit_breaker,
                    f"daily loss {loss_pct:.4f} >= 3%; kill switch engaged",
                )
                return self._reject(proposal, state, "daily loss limit reached, kill switch engaged")

        if proposal.stop_price is None:
            return self._reject(proposal, state, "stop_price required for sizing")
        # Reconcile the bracket up front so sizing uses the SAME stop the order
        # will really use (a hallucinated far stop no longer distorts the size),
        # and so the stored/submitted levels carry the min reward:risk fix.
        recon_stop, recon_target = reconcile_bracket(
            proposal.side, proposal.entry_price, proposal.stop_price,
            proposal.target_price, horizon=proposal.time_horizon,
        )
        qty = size_position(
            state.equity, proposal.entry_price, recon_stop,
            max_position_pct=proposal.proposed_size_pct,
        )

        existing = state.positions.get(proposal.ticker)
        existing_value = (existing.qty * existing.avg_entry_price) if existing else Decimal(0)
        new_total = existing_value + Decimal(qty) * proposal.entry_price
        if new_total > state.equity * MAX_SINGLE_TICKER_PCT:
            return self._reject(proposal, state, "ticker exposure would exceed 20%")

        sector = self.sector_map.get(proposal.ticker, "unknown")
        sector_existing = state.sector_exposure.get(sector, Decimal(0))
        if sector_existing + Decimal(qty) * proposal.entry_price > state.equity * MAX_SECTOR_PCT:
            return self._reject(proposal, state, f"sector {sector} exposure would exceed 40%")

        # Phase 4: sleeve allocation cap.
        sleeve_name = proposal.sleeve.value if hasattr(proposal.sleeve, "value") else str(proposal.sleeve)
        sleeve_current = state.sleeve_exposure.get(sleeve_name, Decimal(0))
        sleeve_reason = proposed_sleeve_breach(
            sleeve_name, sleeve_current, Decimal(qty) * proposal.entry_price, state.equity,
        )
        if sleeve_reason is not None:
            return self._reject(proposal, state, sleeve_reason)

        if qty < 1:
            return self._reject(proposal, state, "computed position size < 1 share")

        if self.history.round_trips_last_7d(proposal.ticker, state.now) >= ROUND_TRIP_LIMIT:
            return self._reject(proposal, state, "5 round trips this week on ticker")

        since = state.now - timedelta(hours=REENTRY_COOLDOWN_HOURS)
        if self.history.had_stop_loss_within(proposal.ticker, since):
            return self._reject(proposal, state, "re-entry cooldown after recent loss")

        sized = SizedProposal(
            **{**proposal.model_dump(), "stop_price": recon_stop, "target_price": recon_target},
            qty=qty,
        )
        return Approved(proposal=sized)

    def _reject(self, proposal: ProposalIn, state: AccountState, reason: str) -> Rejected:
        self._log_event(proposal, state, RiskEventType.rejection, reason)
        return Rejected(reason=reason)

    def _log_event(self, proposal, state, event_type, reason: str) -> None:
        try:
            snap = {
                "equity": str(state.equity),
                "starting_equity_today": str(state.starting_equity_today),
                "cash": str(state.cash),
                "positions": {
                    k: {"qty": str(v.qty), "avg": str(v.avg_entry_price)}
                    for k, v in state.positions.items()
                },
                "sector_exposure": {k: str(v) for k, v in state.sector_exposure.items()},
                "now": state.now.isoformat(),
                "proposal": json.loads(proposal.model_dump_json()),
            }
            ev = RiskEvent(
                event_type=event_type,
                reason=reason,
                related_proposal_id=None,
                account_state_snapshot=snap,
            )
            self.db.add(ev)
            self.db.commit()
        except Exception:
            log.exception("failed to log risk event")
            self.db.rollback()
