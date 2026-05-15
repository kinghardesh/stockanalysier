import json
import logging
from decimal import Decimal
from uuid import uuid4

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import is_kill_switch_active, redis_client
from app.execution import ExecutionService
from app.llm.orchestrator import LLMOrchestrator, NewsEvent
from app.models import Signal, TradeProposal
from app.models.enums import ProposalSide, ProposalTier, SignalSource, TradeSleeve
from app.risk.decision import Approved
from app.risk.engine import RiskEngine
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.account import get_account_state
from app.services.equity import StartOfDayEquityMissing
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)

STREAM_NEWS = "events:news"
CONSUMER_GROUP = "orchestrator_news"
CONSUMER_NAME = f"news_consumer_{uuid4().hex[:8]}"


class NewsConsumer:
    def __init__(
        self,
        orchestrator: LLMOrchestrator,
        risk_engine: RiskEngine,
        executor: ExecutionService,
    ):
        self.orchestrator = orchestrator
        self.risk_engine = risk_engine
        self.executor = executor
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            redis_client.xgroup_create(STREAM_NEWS, CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.exception("xgroup_create failed for %s", STREAM_NEWS)

    async def run_once(self) -> int:
        if is_kill_switch_active():
            return 0

        batch = redis_client.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {STREAM_NEWS: ">"},
            count=settings.llm_batch_size,
            block=settings.llm_batch_timeout_seconds * 1000,
        )
        if not batch:
            return 0

        events: list[NewsEvent] = []
        msg_ids: list[str] = []
        for _, messages in batch:
            for msg_id, fields in messages:
                msg_ids.append(msg_id)
                try:
                    data = json.loads(fields.get("data", "{}"))
                    events.append(NewsEvent(
                        event_id=msg_id,
                        ticker=data.get("ticker", ""),
                        title_sanitized=data.get("title_sanitized", ""),
                        source=data.get("source", ""),
                        url=data.get("url"),
                        published_at=data.get("published_at"),
                    ))
                except Exception:
                    log.exception("failed to parse news event %s", msg_id)

        try:
            proposals = await self.orchestrator.analyze_news_batch(events)
        except Exception:
            log.exception("orchestrator failed; acking batch and dropping")
            if msg_ids:
                redis_client.xack(STREAM_NEWS, CONSUMER_GROUP, *msg_ids)
            return 0

        processed = 0
        for proposal in proposals:
            if proposal is None:
                continue
            try:
                await self._process_proposal(proposal)
                processed += 1
            except Exception:
                log.exception("processing failed for proposal on %s", proposal.ticker)

        if msg_ids:
            redis_client.xack(STREAM_NEWS, CONSUMER_GROUP, *msg_ids)
        return processed

    async def _process_proposal(self, proposal) -> None:
        common = dict(
            ticker=proposal.ticker,
            side=ProposalSide(proposal.side),
            proposed_size_pct=Decimal(str(proposal.proposed_size_pct)),
            stop_price=Decimal(str(proposal.stop_price)),
            target_price=(Decimal(str(proposal.target_price))
                          if proposal.target_price is not None else None),
            thesis=proposal.thesis,
            confidence=proposal.confidence,
            model_used=proposal.model_used,
        )

        with SessionLocal() as db:
            sig = Signal(
                source=SignalSource.news,
                ticker=proposal.ticker,
                signal_type="llm_news",
                raw_data={
                    "news_event_ids": proposal.news_event_ids,
                    "thesis": proposal.thesis,
                    "confidence": proposal.confidence,
                    "invalidation_criteria": proposal.invalidation_criteria,
                    "time_horizon": proposal.time_horizon,
                    "model_used": proposal.model_used,
                },
            )
            db.add(sig)
            db.flush()

            if proposal.tier == "rejected_bear_case":
                row = TradeProposal(
                    signal_id=sig.id, tier=ProposalTier.tier_3,
                    rejected_reason="bear_case_invalidated", **common,
                )
                db.add(row); db.commit()
                log.info("proposal %s rejected by bear case", row.id)
                return

            if proposal.tier == "tier_3":
                row = TradeProposal(signal_id=sig.id, tier=ProposalTier.tier_3, **common)
                db.add(row); db.commit()
                log.info("Tier 3 proposal awaiting approval: %s ticker=%s",
                         row.id, row.ticker)
                return

            tier_enum = (ProposalTier.tier_1 if proposal.tier == "tier_1"
                         else ProposalTier.tier_2)
            row = TradeProposal(signal_id=sig.id, tier=tier_enum, **common)
            db.add(row); db.commit()

            try:
                state = get_account_state()
            except StartOfDayEquityMissing:
                log.error("aborting llm-news proposal %s: SOD equity missing", row.id)
                return

            try:
                entry_price = latest_trade_price(proposal.ticker)
            except Exception:
                log.exception("could not fetch latest price for %s; fallback heuristic",
                              proposal.ticker)
                entry_price = Decimal(str(proposal.stop_price)) * Decimal("1.05")

            proposal_in = ProposalIn(
                signal_id=sig.id,
                ticker=proposal.ticker,
                side=ProposalSide(proposal.side),
                entry_price=entry_price,
                stop_price=Decimal(str(proposal.stop_price)),
                target_price=(Decimal(str(proposal.target_price))
                              if proposal.target_price else None),
                thesis=proposal.thesis,
                confidence=proposal.confidence,
                model_used=proposal.model_used,
                tier=tier_enum,
                sleeve=TradeSleeve(proposal.sleeve or "discretionary"),
            )
            decision = self.risk_engine.validate(proposal_in, state)
            persist_decision(db, row, decision)
            if isinstance(decision, Approved):
                await self.executor.submit(decision.proposal)
            else:
                log.info("LLM news proposal for %s rejected by risk: %s",
                         proposal.ticker, decision.reason)
