import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.execution import ExecutionService
from app.models import Signal, SignalSource, TradeProposal
from app.models.enums import ProposalSide, ProposalTier, TradeSleeve
from app.risk.decision import Approved
from app.risk.engine import RiskEngine
from app.risk.history import AccountState, DBTradeHistory, PositionSnapshot
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.equity import StartOfDayEquityMissing, read_sod_equity

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SYMBOLS = ["SPY", "QQQ", "IWM"]


class TrendSignalEngine:
    def __init__(self, db, risk_engine: RiskEngine, executor: ExecutionService, account_state_provider):
        self.db = db
        self.risk_engine = risk_engine
        self.executor = executor
        self.account_state_provider = account_state_provider
        self.data_client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)

    @classmethod
    def build(cls):
        db = SessionLocal()
        history = DBTradeHistory(db)
        risk = RiskEngine(db=db, whitelist=WHITELIST + SYMBOLS,
                          sector_map=SECTOR_MAP, history=history)
        return cls(db, risk, ExecutionService(), _alpaca_account_state)

    def _bars(self, symbol: str, lookback_days: int = 365) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        # IEX feed is the only one available on Alpaca's free paper tier. SIP
        # (the default) returns 403: "subscription does not permit querying
        # recent SIP data". IEX gives daily bars going back years — fine for
        # 50/200 SMA crossover.
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=start, end=end, feed=DataFeed.IEX,
        )
        df = self.data_client.get_stock_bars(req).df
        if df.empty:
            return df
        if "symbol" in df.index.names:
            df = df.xs(symbol, level="symbol")
        return df.tail(250)

    async def scan(self) -> int:
        try:
            state = self.account_state_provider()
        except StartOfDayEquityMissing:
            log.error("aborting trend scan: SOD equity missing; kill switch engaged")
            return 0

        emitted = 0
        for sym in SYMBOLS:
            df = self._bars(sym)
            if len(df) < 200:
                log.info("skip %s: insufficient bars (%d)", sym, len(df))
                continue
            sma50 = df["close"].rolling(50).mean()
            sma200 = df["close"].rolling(200).mean()
            cross_today = (sma50.iloc[-1] > sma200.iloc[-1]) and (sma50.iloc[-2] <= sma200.iloc[-2])
            above_both = (df["close"].iloc[-1] > sma50.iloc[-1]) and (df["close"].iloc[-1] > sma200.iloc[-1])
            if not (cross_today and above_both):
                continue

            today_close = Decimal(str(df["close"].iloc[-1]))
            stop = Decimal(str(sma200.iloc[-1]))

            sig = Signal(
                source=SignalSource.sma_crossover,
                ticker=sym,
                signal_type="sma_50_200_golden_cross",
                raw_data={
                    "close": float(df["close"].iloc[-1]),
                    "sma_50": float(sma50.iloc[-1]),
                    "sma_200": float(sma200.iloc[-1]),
                },
            )
            self.db.add(sig)
            self.db.flush()

            proposal_in = ProposalIn(
                signal_id=sig.id,
                ticker=sym,
                side=ProposalSide.buy,
                entry_price=today_close,
                stop_price=stop,
                thesis="SMA(50) crossed above SMA(200), price above both",
                confidence=10,
                model_used="mechanical_sma_50_200",
                tier=ProposalTier.tier_1,
                sleeve=TradeSleeve.trend,
            )
            proposal_row = TradeProposal(
                signal_id=sig.id,
                ticker=sym,
                side=proposal_in.side,
                stop_price=proposal_in.stop_price,
                target_price=None,
                thesis=proposal_in.thesis,
                confidence=proposal_in.confidence,
                model_used=proposal_in.model_used,
                tier=proposal_in.tier,
            )
            self.db.add(proposal_row)
            self.db.commit()

            decision = self.risk_engine.validate(proposal_in, state)
            persist_decision(self.db, proposal_row, decision)

            if isinstance(decision, Approved):
                await self.executor.submit(decision.proposal)
                emitted += 1
            else:
                log.info("trend proposal for %s rejected: %s", sym, decision.reason)
        return emitted


def _alpaca_account_state() -> AccountState:
    starting_eq = read_sod_equity()

    client = TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)
    acct = client.get_account()
    positions_list = client.get_all_positions()
    positions: dict[str, PositionSnapshot] = {}
    sector_exposure: dict[str, Decimal] = {}
    marks: dict[str, Decimal] = {}  # ticker -> current mark (for sleeve exposure)
    for p in positions_list:
        positions[p.symbol] = PositionSnapshot(
            ticker=p.symbol,
            qty=Decimal(str(p.qty)),
            avg_entry_price=Decimal(str(p.avg_entry_price)),
        )
        mark_price = Decimal(str(p.current_price or p.avg_entry_price))
        marks[p.symbol] = mark_price
        sector = SECTOR_MAP.get(p.symbol, "unknown")
        sector_exposure[sector] = sector_exposure.get(sector, Decimal(0)) + (
            Decimal(str(p.qty)) * mark_price
        )

    # Sleeve exposure: Alpaca doesn't know which sleeve owns a ticker. We tracked
    # the sleeve on Position when we opened the trade, so join here.
    sleeve_exposure: dict[str, Decimal] = {}
    from sqlalchemy import select as _select
    from app.core.db import SessionLocal as _SessionLocal
    from app.models import Position as _PositionModel
    with _SessionLocal() as _db:
        for row in _db.execute(_select(_PositionModel)).scalars().all():
            sleeve_name = row.sleeve.value if hasattr(row.sleeve, "value") else str(row.sleeve)
            mark = marks.get(row.ticker, row.avg_entry_price)
            sleeve_exposure[sleeve_name] = sleeve_exposure.get(sleeve_name, Decimal(0)) + (
                row.qty * mark
            )

    return AccountState(
        equity=Decimal(str(acct.equity)),
        starting_equity_today=starting_eq,
        cash=Decimal(str(acct.cash)),
        positions=positions,
        sector_exposure=sector_exposure,
        sleeve_exposure=sleeve_exposure,
        now=datetime.now(ET),
    )
