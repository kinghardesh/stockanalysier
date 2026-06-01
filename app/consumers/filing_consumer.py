import asyncio
import json
import logging
from decimal import Decimal
from uuid import uuid4

from app.core.db import SessionLocal
from app.core.redis import is_kill_switch_active, redis_client
from app.execution import ExecutionService
from app.llm.orchestrator import FilingEvent, LLMOrchestrator
from app.models import Signal, TradeProposal
from app.models.enums import ProposalSide, ProposalTier, SignalSource, TimeHorizon, TradeSleeve
from app.risk.decision import Approved
from app.risk.engine import RiskEngine
from app.risk.persistence import persist_decision
from app.schemas import ProposalIn
from app.services.account import get_account_state
from app.services.equity import StartOfDayEquityMissing
from app.services.quotes import latest_trade_price

log = logging.getLogger(__name__)

STREAM_FILINGS = "events:filings"
CONSUMER_GROUP = "orchestrator_filings"
CONSUMER_NAME = f"filing_consumer_{uuid4().hex[:8]}"


class FilingConsumer:
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
            redis_client.xgroup_create(STREAM_FILINGS, CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.exception("xgroup_create failed for %s", STREAM_FILINGS)

    async def run_once(self) -> int:
        if is_kill_switch_active():
            return 0
        # Blocking long-poll read off the event loop (see news_consumer).
        batch = await asyncio.to_thread(
            redis_client.xreadgroup,
            CONSUMER_GROUP, CONSUMER_NAME,
            {STREAM_FILINGS: ">"},
            count=1,
            block=30_000,
        )
        if not batch:
            return 0

        processed = 0
        for _, messages in batch:
            for msg_id, fields in messages:
                try:
                    data = json.loads(fields.get("data", "{}"))
                    event = FilingEvent(
                        event_id=msg_id,
                        ticker=data.get("ticker", ""),
                        cik=data.get("cik", ""),
                        form_type=data.get("form_type", ""),
                        accession=data.get("accession", ""),
                        filed_at=data.get("filed_at", ""),
                        url=data.get("url", ""),
                        excerpt=data.get("excerpt"),
                    )
                    proposal = await self.orchestrator.analyze_filing(event)
                    if proposal is not None:
                        await self._process_proposal(proposal, event)
                        processed += 1
                except Exception:
                    log.exception("failed to process filing %s", msg_id)
                finally:
                    redis_client.xack(STREAM_FILINGS, CONSUMER_GROUP, msg_id)
        return processed

    async def _process_proposal(self, proposal, event: FilingEvent) -> None:
        horizon = TimeHorizon(proposal.time_horizon) if proposal.time_horizon else None
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
            time_horizon=horizon,
        )
        with SessionLocal() as db:
            sig = Signal(
                source=SignalSource.filing,
                ticker=proposal.ticker,
                signal_type=f"llm_filing_{event.form_type}",
                raw_data={
                    "accession": event.accession,
                    "filing_url": event.url,
                    "filed_at": event.filed_at,
                    "thesis": proposal.thesis,
                    "confidence": proposal.confidence,
                    "invalidation_criteria": proposal.invalidation_criteria,
                    "time_horizon": proposal.time_horizon,
                    "model_used": proposal.model_used,
                    "verifier_verdict": proposal.verifier_verdict,
                    "verifier_model": proposal.verifier_model,
                    "verifier_confidence": proposal.verifier_confidence,
                    "verifier_reasoning": proposal.verifier_reasoning,
                },
            )
            db.add(sig); db.flush()

            if proposal.tier == "rejected_bear_case":
                row = TradeProposal(
                    signal_id=sig.id, tier=ProposalTier.tier_3,
                    rejected_reason="bear_case_invalidated", **common,
                )
                db.add(row); db.commit()
                log.info("filing proposal %s rejected by bear case", row.id)
                return
            if proposal.tier == "tier_3":
                row = TradeProposal(signal_id=sig.id, tier=ProposalTier.tier_3, **common)
                db.add(row); db.commit()
                log.info("Tier 3 filing proposal awaiting approval: %s ticker=%s",
                         row.id, row.ticker)
                return

            tier_enum = (ProposalTier.tier_1 if proposal.tier == "tier_1"
                         else ProposalTier.tier_2)
            row = TradeProposal(signal_id=sig.id, tier=tier_enum, **common)
            db.add(row); db.commit()

            try:
                state = get_account_state()
            except StartOfDayEquityMissing:
                log.error("aborting llm-filing proposal %s: SOD equity missing", row.id)
                return

            try:
                entry_price = latest_trade_price(proposal.ticker)
            except Exception:
                log.exception("latest price fetch failed for %s; heuristic fallback",
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
                time_horizon=horizon,
                proposed_size_pct=Decimal(str(proposal.proposed_size_pct)),
            )
            decision = self.risk_engine.validate(proposal_in, state)
            persist_decision(db, row, decision)
            if isinstance(decision, Approved):
                await self.executor.submit(decision.proposal)
            else:
                log.info("LLM filing proposal for %s rejected by risk: %s",
                         proposal.ticker, decision.reason)
