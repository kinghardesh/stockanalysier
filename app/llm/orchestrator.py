import json
import logging
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.redis import is_kill_switch_active
from app.llm.prompts import (
    BEAR_CASE_PROMPT_TEMPLATE, CANDIDATE_PROMPT_TEMPLATE, FILING_PROMPT_TEMPLATE,
    NEWS_PROMPT_TEMPLATE, SYSTEM_PROMPT, VERIFIER_PROMPT_TEMPLATE,
)
from app.llm.providers.base import (
    LLMProvider, ProviderError, RateLimitError, SchemaValidationError,
)
from app.llm.rate_limiter import RedisRateLimiter
from app.llm.schemas import (
    BearCaseAssessment, CandidateAssessment, LLMTradeProposal, VerifierVerdict,
    bear_case_schema, candidate_assessment_schema, llm_facing_filing_schema,
    llm_facing_news_batch_schema, verifier_schema,
)

log = logging.getLogger(__name__)


class NoAnalysisAvailable(Exception):
    """All providers in the chain failed; caller must skip, never guess."""


class NewsEvent(BaseModel):
    event_id: str
    ticker: str
    title_sanitized: str
    source: str
    url: Optional[str] = None
    published_at: Optional[str] = None


class FilingEvent(BaseModel):
    event_id: str
    ticker: str
    cik: str
    form_type: str
    accession: str
    filed_at: str
    url: str
    excerpt: Optional[str] = None


class LLMOrchestrator:
    def __init__(
        self,
        providers: list[LLMProvider],
        rate_limiter: RedisRateLimiter,
        account_state_provider: Callable[[], dict],
        verifier: Optional[LLMProvider] = None,
    ):
        self.providers = providers
        self.rate_limiter = rate_limiter
        self.account_state_provider = account_state_provider
        # Independent second-opinion model (GPT). Not in the generation chain.
        self.verifier = verifier
        self._by_name = {p.name: p for p in providers}

    async def analyze_news_batch(
        self, events: list[NewsEvent]
    ) -> list[Optional[LLMTradeProposal]]:
        tickers = _unique_tickers(events)
        if not events:
            return []
        if is_kill_switch_active():
            log.info("kill switch ON; skipping news batch")
            return [None] * len(tickers)
        try:
            state = self.account_state_provider()
        except Exception as e:
            log.warning("state provider failed: %s; skipping news batch", e)
            return [None] * len(tickers)

        schema = llm_facing_news_batch_schema()
        prices = self._fetch_prices(tickers)
        prompt = self._build_news_prompt(events, state, schema, prices)

        def parse(raw: dict) -> list[LLMTradeProposal]:
            return [LLMTradeProposal(**p) for p in (raw.get("proposals") or [])]

        proposals, model_used = await self._call_with_retry(prompt, schema, parse)
        if not proposals:
            return [None] * len(tickers)

        by_ticker: dict[str, LLMTradeProposal] = {}
        for p in proposals:
            p.model_used = model_used
            p.sleeve = "discretionary"
            await self._assign_tier(p, source="news", model_used=model_used)
            by_ticker[p.ticker] = p
        return [by_ticker.get(t) for t in tickers]

    async def analyze_filing(self, event: FilingEvent) -> Optional[LLMTradeProposal]:
        if is_kill_switch_active():
            log.info("kill switch ON; skipping filing %s", event.event_id)
            return None
        try:
            state = self.account_state_provider()
        except Exception as e:
            log.warning("state provider failed: %s; skipping filing", e)
            return None

        schema = llm_facing_filing_schema()
        prices = self._fetch_prices([event.ticker])
        prompt = self._build_filing_prompt(event, state, schema, prices)

        def parse(raw: dict) -> Optional[LLMTradeProposal]:
            if raw.get("no_signal") is True:
                return None
            payload = raw.get("proposal")
            if not payload:
                return None
            return LLMTradeProposal(**payload)

        proposal, model_used = await self._call_with_retry(prompt, schema, parse)
        if proposal is None:
            return None
        proposal.model_used = model_used
        proposal.sleeve = "discretionary"
        await self._assign_tier(proposal, source="filing", model_used=model_used)
        return proposal

    async def analyze_candidate(self, candidate: dict) -> Optional[CandidateAssessment]:
        """Vet a mechanically-screened buy candidate. Returns the assessment
        (which may be buy=false) or None if all providers failed."""
        if is_kill_switch_active():
            return None
        schema = candidate_assessment_schema()
        prompt = CANDIDATE_PROMPT_TEMPLATE.format(
            ticker=candidate.get("ticker"),
            signal=candidate.get("signal"),
            price=candidate.get("price"),
            sma50=candidate.get("sma50"),
            sma200=candidate.get("sma200"),
            rsi=candidate.get("rsi"),
            sector=candidate.get("sector") or "unknown",
            market_cap=candidate.get("market_cap") or "n/a",
            pe=candidate.get("pe") or "n/a",
            schema_json=json.dumps(schema),
        )

        def parse(raw: dict) -> CandidateAssessment:
            return CandidateAssessment(**raw)

        assessment, _ = await self._call_with_retry(prompt, schema, parse)
        return assessment

    async def _call_with_retry(self, prompt: str, schema: dict, parser):
        last_error = None
        for attempt in (1, 2):
            effective = prompt if attempt == 1 else (
                f"{prompt}\n\nYour previous response failed validation:\n"
                f"{last_error}\n\nProduce a corrected JSON response now."
            )
            try:
                raw, model_used = await self._call_with_fallback(effective, schema)
            except NoAnalysisAvailable:
                self._log_llm_failure("llm_unavailable")
                return None, None
            try:
                return parser(raw), model_used
            except (ValidationError, ValueError) as e:
                last_error = _format_error(e)
                log.warning("LLM parse failure attempt %d: %s", attempt, last_error)
        self._log_llm_failure("llm_schema_validation_failed")
        return None, None

    async def _call_with_fallback(self, prompt: str, schema: dict) -> tuple[dict, str]:
        chain = [self._by_name[n] for n in settings.llm_provider_chain if n in self._by_name]
        if not chain:
            raise NoAnalysisAvailable("provider chain is empty")
        for provider in chain:
            if not await self.rate_limiter.allow(provider.name):
                log.info("rate limited %s; falling through", provider.name)
                continue
            try:
                raw = await provider.generate_structured(prompt, schema)
                return raw, provider.name
            except RateLimitError:
                log.warning("%s 429 at runtime; falling through", provider.name)
            except SchemaValidationError as e:
                log.warning("%s schema mismatch: %s; falling through", provider.name, e)
            except ProviderError as e:
                log.warning("%s provider error: %s; falling through", provider.name, e)
        raise NoAnalysisAvailable("no provider in chain succeeded")

    async def _assign_tier(
        self, proposal: LLMTradeProposal, *, source: str, model_used: str
    ) -> None:
        tier = _compute_tier(proposal, source)
        if tier == "tier_3":
            assessment = await self._devils_advocate(proposal)
            if assessment is not None and assessment.strength in ("weak", "very_weak"):
                proposal.tier = "rejected_bear_case"
                log.info("bear case (%s) invalidated tier_3 proposal for %s",
                         assessment.strength, proposal.ticker)
                return
        proposal.tier = tier
        # GPT tie-breaker: a second, independent opinion on borderline trades
        # that would otherwise auto-execute. May downgrade tier to tier_3.
        await self._gpt_tiebreaker(proposal)

    def _verifier_eligible(self, proposal: LLMTradeProposal) -> bool:
        if self.verifier is None or not settings.gpt_verifier_enabled:
            return False
        # Only proposals that would auto-execute, and only when borderline by
        # confidence. tier_3 already goes to a human; rejected ones are dead.
        if proposal.tier not in ("tier_1", "tier_2"):
            return False
        return proposal.confidence <= settings.gpt_verifier_confidence_ceiling

    async def _gpt_tiebreaker(self, proposal: LLMTradeProposal) -> None:
        if not self._verifier_eligible(proposal):
            return
        verdict = await self._verify(proposal)
        if verdict is None:
            # Resilient: a verifier failure must never block the pipeline; the
            # original decision stands, flagged so it's visible downstream.
            proposal.verifier_verdict = "error"
            proposal.verifier_model = getattr(self.verifier, "model", self.verifier.name)
            return
        proposal.verifier_verdict = "agree" if verdict.agree else "disagree"
        proposal.verifier_model = getattr(self.verifier, "model", self.verifier.name)
        proposal.verifier_confidence = verdict.confidence
        proposal.verifier_reasoning = verdict.reasoning
        if not verdict.agree:
            log.info("GPT tie-breaker DISAGREED on %s (%s); downgrading %s -> tier_3",
                     proposal.ticker, verdict.reasoning[:80], proposal.tier)
            proposal.tier = "tier_3"

    async def _verify(self, proposal: LLMTradeProposal) -> Optional[VerifierVerdict]:
        schema = verifier_schema()
        prompt = VERIFIER_PROMPT_TEMPLATE.format(
            ticker=proposal.ticker,
            side=proposal.side,
            confidence=proposal.confidence,
            proposed_size_pct=proposal.proposed_size_pct,
            stop_price=proposal.stop_price,
            target_price=proposal.target_price,
            time_horizon=proposal.time_horizon,
            thesis=proposal.thesis,
            invalidation_criteria=proposal.invalidation_criteria,
            schema_json=json.dumps(schema),
        )
        try:
            raw = await self.verifier.generate_structured(
                prompt, schema, timeout=settings.gpt_verifier_timeout_seconds,
            )
        except ProviderError as e:
            log.warning("GPT verifier unavailable for %s: %s; keeping original decision",
                        proposal.ticker, e)
            return None
        try:
            return VerifierVerdict(**raw)
        except (ValidationError, ValueError) as e:
            log.warning("GPT verifier bad response for %s: %s", proposal.ticker, _format_error(e))
            return None

    async def _devils_advocate(self, proposal: LLMTradeProposal) -> Optional[BearCaseAssessment]:
        schema = bear_case_schema()
        prompt = BEAR_CASE_PROMPT_TEMPLATE.format(
            ticker=proposal.ticker,
            side=proposal.side,
            thesis=proposal.thesis,
            confidence=proposal.confidence,
            stop_price=proposal.stop_price,
            time_horizon=proposal.time_horizon,
            invalidation_criteria=proposal.invalidation_criteria,
            schema_json=json.dumps(schema),
        )

        def parse(raw: dict) -> BearCaseAssessment:
            return BearCaseAssessment(**raw)

        assessment, _ = await self._call_with_retry(prompt, schema, parse)
        if assessment is None:
            log.warning("devil's advocate failed for %s; tier_3 will pass through",
                        proposal.ticker)
        return assessment

    def _fetch_prices(self, tickers: list[str]) -> dict[str, str]:
        """Fetch live IEX prices so the LLM anchors stop/target to reality
        instead of hallucinating levels from stale training data."""
        from app.services.quotes import latest_trade_price
        out: dict[str, str] = {}
        for t in tickers:
            try:
                out[t] = f"{latest_trade_price(t):.2f}"
            except Exception:
                log.warning("live price unavailable for %s", t)
        return out

    @staticmethod
    def _format_prices(prices: dict[str, str]) -> str:
        if not prices:
            return "unavailable (do not propose price-specific levels)"
        return ", ".join(f"{t}=${p}" for t, p in prices.items())

    def _build_news_prompt(self, events: list[NewsEvent], state: dict, schema: dict,
                           prices: dict[str, str]) -> str:
        news_block = "\n".join(
            f"- event_id={e.event_id} ticker={e.ticker} source={e.source} "
            f"published={e.published_at or '?'} :: {e.title_sanitized}"
            for e in events
        )
        return NEWS_PROMPT_TEMPLATE.format(
            system_prompt=SYSTEM_PROMPT.format(
                whitelist=", ".join(settings.whitelist_tickers)
            ),
            equity=state.get("equity", "?"),
            buying_power=state.get("buying_power", "?"),
            sod_equity=state.get("sod_equity", "?"),
            daily_pnl=state.get("daily_pnl", "?"),
            daily_pnl_pct=state.get("daily_pnl_pct", "?"),
            kill_switch_status=state.get("kill_switch_status", "UNKNOWN"),
            open_positions=state.get("open_positions", "?"),
            recent_fills=state.get("recent_fills", "?"),
            current_prices=self._format_prices(prices),
            news_block=news_block,
            schema_json=json.dumps(schema),
        )

    def _build_filing_prompt(self, event: FilingEvent, state: dict, schema: dict,
                             prices: dict[str, str]) -> str:
        return FILING_PROMPT_TEMPLATE.format(
            system_prompt=SYSTEM_PROMPT.format(
                whitelist=", ".join(settings.whitelist_tickers)
            ),
            equity=state.get("equity", "?"),
            buying_power=state.get("buying_power", "?"),
            sod_equity=state.get("sod_equity", "?"),
            daily_pnl=state.get("daily_pnl", "?"),
            daily_pnl_pct=state.get("daily_pnl_pct", "?"),
            kill_switch_status=state.get("kill_switch_status", "UNKNOWN"),
            open_positions=state.get("open_positions", "?"),
            recent_fills=state.get("recent_fills", "?"),
            current_prices=self._format_prices(prices),
            ticker=event.ticker,
            form_type=event.form_type,
            filed_at=event.filed_at,
            filing_url=event.url,
            excerpt=event.excerpt or "(no excerpt; reviewer should fetch URL if material)",
            schema_json=json.dumps(schema),
        )

    def _log_llm_failure(self, reason: str) -> None:
        try:
            from app.core.db import SessionLocal
            from app.models import RiskEvent, RiskEventType
            with SessionLocal() as db:
                db.add(RiskEvent(
                    event_type=RiskEventType.rejection,
                    reason=f"llm:{reason}",
                    related_proposal_id=None,
                    account_state_snapshot={},
                ))
                db.commit()
        except Exception:
            log.exception("could not write llm failure to risk_events")


def _unique_tickers(events: list[NewsEvent]) -> list[str]:
    seen: list[str] = []
    for e in events:
        if e.ticker and e.ticker not in seen:
            seen.append(e.ticker)
    return seen


def _compute_tier(p: LLMTradeProposal, source: str) -> str:
    c, size = p.confidence, p.proposed_size_pct
    if (c >= 8 and size > 0.005) or (source == "filing" and c >= 8 and size > 0.003):
        return "tier_3"
    if c >= 8 and size <= 0.005 and source == "news":
        return "tier_1"
    if (6 <= c <= 7) or (source == "filing" and size <= 0.005):
        return "tier_2"
    return "tier_2"


def _format_error(e) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in e.errors()
        )[:500]
    return str(e)[:500]


def default_account_state_provider() -> dict:
    from app.services.account import get_llm_state_dict
    return get_llm_state_dict()
