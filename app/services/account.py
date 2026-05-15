from decimal import Decimal

from app.core.redis import is_kill_switch_active
from app.risk.history import AccountState
from app.signals.trend import _alpaca_account_state


def get_account_state() -> AccountState:
    """Strict AccountState for the risk engine. Raises StartOfDayEquityMissing
    if equity:start_of_day is absent."""
    return _alpaca_account_state()


def get_llm_state_dict() -> dict:
    """String-formatted dict for LLM prompt templates."""
    state = _alpaca_account_state()
    daily_pnl = state.equity - state.starting_equity_today
    daily_pct = (
        (daily_pnl / state.starting_equity_today * Decimal(100))
        if state.starting_equity_today else Decimal(0)
    )
    positions_str = ", ".join(
        f"{t}(qty={s.qty}, avg=${s.avg_entry_price})"
        for t, s in state.positions.items()
    ) or "none"
    return {
        "equity": f"{state.equity:.2f}",
        "buying_power": f"{state.cash:.2f}",
        "sod_equity": f"{state.starting_equity_today:.2f}",
        "daily_pnl": f"{daily_pnl:.2f}",
        "daily_pnl_pct": f"{daily_pct:.2f}",
        "kill_switch_status": "ON" if is_kill_switch_active() else "OFF",
        "open_positions": positions_str,
        "recent_fills": "n/a in phase 3",
    }
