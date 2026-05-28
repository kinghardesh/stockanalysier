"""Mean reversion signal engine — Phase 4.

RSI(2) entry on whitelist stocks above their 200 SMA.
  Entry condition:  RSI(2) < 10 AND close > SMA(200)
  Exit condition:   RSI(2) > 70 (handled by position monitor against a target)
  Sleeve:           mean_reversion (15% capital cap enforced by risk engine)
  Tier:             1 (auto-execute) — mechanical, deterministic, high confidence

Universe defaults to the whitelist. settings.mean_reversion_universe="sp500"
will be wired in Phase 5 alongside the data-tier upgrade.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.execution import ExecutionService
from app.models import Signal, SignalSource, TradeProposal
from app.models.enums import ProposalSide, ProposalTier, TradeSleeve
from app.risk.decision import Approved
from app.risk.engine import RiskEngine
from app.risk.history import DBTradeHistory
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.equity import StartOfDayEquityMissing

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

RSI_PERIOD = 2
RSI_ENTRY_THRESHOLD = 10
RSI_EXIT_THRESHOLD = 70  # used by position-monitor extension (Phase 5 work; we set target via target_price)
SMA_TREND_FILTER = 200
LOOKBACK_DAYS = 365


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


class MeanReversionSignalEngine:
    def __init__(
        self,
        db,
        risk_engine: RiskEngine,
        executor: ExecutionService,
        account_state_provider,
        universe: list[str] | None = None,
    ):
        self.db = db
        self.risk_engine = risk_engine
        self.executor = executor
        self.account_state_provider = account_state_provider
        self.universe = universe or self._resolve_universe()
        self.data_client = StockHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_api_secret,
        )

    @staticmethod
    def _resolve_universe() -> list[str]:
        if settings.mean_reversion_universe == "sp500":
            # Phase 5: replace with a curated SP500 constituent list.
            log.warning("mean_reversion_universe=sp500 requested; SP500 not wired until Phase 5. Using whitelist.")
        return list(WHITELIST)

    @classmethod
    def build(cls):
        from app.services.account import get_account_state
        db = SessionLocal()
        history = DBTradeHistory(db)
        risk = RiskEngine(
            db=db, whitelist=WHITELIST, sector_map=SECTOR_MAP, history=history,
        )
        return cls(db, risk, ExecutionService(), get_account_state)

    def _bars(self, symbol: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=LOOKBACK_DAYS)
        # IEX feed: free paper tier doesn't permit SIP. See trend.py for context.
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=start, end=end, feed=DataFeed.IEX,
        )
        df = self.data_client.get_stock_bars(req).df
        if df.empty:
            return df
        if "symbol" in df.index.names:
            df = df.xs(symbol, level="symbol")
        return df.tail(LOOKBACK_DAYS)

    async def scan(self) -> int:
        try:
            state = self.account_state_provider()
        except StartOfDayEquityMissing:
            log.error("aborting mean-reversion scan: SOD equity missing; kill switch engaged")
            return 0

        emitted = 0
        for symbol in self.universe:
            try:
                df = self._bars(symbol)
            except Exception:
                log.exception("bars fetch failed for %s", symbol)
                continue
            if len(df) < SMA_TREND_FILTER + RSI_PERIOD:
                continue

            close = df["close"]
            rsi = _rsi(close, RSI_PERIOD)
            sma200 = close.rolling(SMA_TREND_FILTER).mean()

            rsi_today = float(rsi.iloc[-1])
            close_today = float(close.iloc[-1])
            sma_today = float(sma200.iloc[-1])

            entry = (rsi_today < RSI_ENTRY_THRESHOLD) and (close_today > sma_today)
            if not entry:
                continue

            # Conservative stop: 2 ATR-ish proxy via recent 5-day low.
            stop = float(close.tail(5).min()) * 0.98
            if stop >= close_today:
                # avoid degenerate sizing when stop is above entry due to noise
                log.info("skip %s: degenerate stop %.4f >= close %.4f", symbol, stop, close_today)
                continue

            # Target: revert to SMA(200) — typical mean-reversion exit.
            target = sma_today

            sig = Signal(
                source=SignalSource.rsi_mean_reversion,
                ticker=symbol,
                signal_type="rsi2_below_10_above_sma200",
                raw_data={
                    "rsi_2": rsi_today,
                    "close": close_today,
                    "sma_200": sma_today,
                    "stop": stop,
                    "target": target,
                },
            )
            self.db.add(sig); self.db.flush()

            proposal_in = ProposalIn(
                signal_id=sig.id,
                ticker=symbol,
                side=ProposalSide.buy,
                entry_price=Decimal(str(close_today)),
                stop_price=Decimal(str(stop)),
                target_price=Decimal(str(target)),
                thesis=(
                    f"RSI(2)={rsi_today:.2f} < 10, close > SMA(200). "
                    f"Mean reversion target = SMA(200)."
                ),
                confidence=10,
                model_used="mechanical_rsi2",
                tier=ProposalTier.tier_1,
                sleeve=TradeSleeve.mean_reversion,
            )

            proposal_row = TradeProposal(
                signal_id=sig.id,
                ticker=symbol,
                side=proposal_in.side,
                stop_price=proposal_in.stop_price,
                target_price=proposal_in.target_price,
                thesis=proposal_in.thesis,
                confidence=proposal_in.confidence,
                model_used=proposal_in.model_used,
                tier=proposal_in.tier,
            )
            self.db.add(proposal_row); self.db.commit()

            decision = self.risk_engine.validate(proposal_in, state)
            persist_decision(self.db, proposal_row, decision)

            if isinstance(decision, Approved):
                try:
                    await self.executor.submit_bracket(decision.proposal)
                    emitted += 1
                except Exception:
                    log.exception("execution failed for mean-reversion %s", symbol)
            else:
                log.info("mean-reversion %s rejected: %s", symbol, decision.reason)
        return emitted
