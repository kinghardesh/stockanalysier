"""Tier 3 approval timeout — with GPT fallback approver.

Pending Tier 3 proposals expire after TIER3_APPROVAL_TIMEOUT_MINUTES (default
30). News/8-Ks have a half-life, so a stale unapproved proposal shouldn't sit
forever. Historically these were auto-rejected. Now, if the operator didn't
approve in time, GPT acts as a backup decision-maker:

  - GPT judges it a good deal  -> risk-validate + execute it
  - GPT judges it weak         -> reject ("gpt_timeout_rejected")
  - GPT unavailable / disabled -> reject ("hitl_timeout")  (original behavior)

A proposal is pending iff: tier=tier_3, rejected_reason IS NULL, and it has no
trade row in (pending, partial, filled). Runs every 5 minutes.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import not_, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.llm.verifier import gpt_verdict
from app.models import Signal, Trade, TradeProposal
from app.models.enums import ProposalTier, TradeStatus
from app.services.approval import approve_and_execute

log = logging.getLogger(__name__)


def _stale_candidates(cutoff):
    """Return list of (proposal_id, gpt_details_dict) for stale pending tier-3."""
    items = []
    with SessionLocal() as db:
        candidates = db.execute(
            select(TradeProposal).where(
                TradeProposal.tier == ProposalTier.tier_3,
                TradeProposal.rejected_reason.is_(None),
                TradeProposal.created_at < cutoff,
                not_(
                    TradeProposal.id.in_(
                        select(Trade.proposal_id).where(
                            Trade.status.in_([
                                TradeStatus.pending, TradeStatus.partial, TradeStatus.filled,
                            ])
                        )
                    )
                ),
            )
        ).scalars().all()
        for prop in candidates:
            sig = db.get(Signal, prop.signal_id)
            inval = (sig.raw_data or {}).get("invalidation_criteria", "") if sig else ""
            items.append((prop.id, {
                "ticker": prop.ticker,
                "side": prop.side.value,
                "confidence": prop.confidence,
                "proposed_size_pct": float(prop.proposed_size_pct or 0),
                "stop_price": float(prop.stop_price or 0),
                "target_price": float(prop.target_price) if prop.target_price else None,
                "time_horizon": prop.time_horizon.value if prop.time_horizon else "swing",
                "thesis": prop.thesis,
                "invalidation_criteria": inval,
            }))
    return items


def _reject(proposal_id, reason: str) -> None:
    with SessionLocal() as db:
        prop = db.get(TradeProposal, proposal_id)
        if prop is not None and prop.rejected_reason is None:
            prop.rejected_reason = reason
            db.commit()


def expire_stale_tier3_proposals() -> int:
    """Plain (no-GPT) timeout reject of stale pending Tier-3. Returns count.

    Kept as the simple synchronous path; the GPT fallback approver lives in
    run_once(). Rejects with 'hitl_timeout', preserving the original contract.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.tier3_approval_timeout_minutes)
    items = _stale_candidates(cutoff)
    for pid, _ in items:
        _reject(pid, "hitl_timeout")
    if items:
        log.info("expire_stale_tier3 (plain): rejected %d stale proposals", len(items))
    return len(items)


async def run_once() -> dict:
    result = {"stale": 0, "gpt_approved": 0, "gpt_rejected": 0, "timeout_rejected": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.tier3_approval_timeout_minutes)

    items = _stale_candidates(cutoff)
    result["stale"] = len(items)
    use_gpt = settings.gpt_timeout_approver_enabled and bool(settings.openai_api_key)

    for pid, details in items:
        verdict = await gpt_verdict(**details) if use_gpt else None

        if verdict is None:
            # GPT disabled/unavailable -> original behavior: reject as stale.
            _reject(pid, "hitl_timeout")
            result["timeout_rejected"] += 1
            continue

        if not verdict.agree:
            _reject(pid, "gpt_timeout_rejected")
            result["gpt_rejected"] += 1
            log.info("GPT rejected stale tier3 %s: %s", pid, (verdict.reasoning or "")[:100])
            continue

        # GPT approves -> risk-validate + execute.
        try:
            ok, reason = await approve_and_execute(pid)
        except Exception:
            log.exception("approve_and_execute failed for %s", pid)
            ok, reason = False, "execute error"
        if ok:
            result["gpt_approved"] += 1
            log.info("GPT auto-approved stale tier3 %s: %s", pid, (verdict.reasoning or "")[:100])
        else:
            _reject(pid, f"gpt_ok_risk_blocked:{reason}"[:200])
            result["gpt_rejected"] += 1
            log.info("GPT approved %s but risk blocked: %s", pid, reason)

    if any(v for k, v in result.items() if k != "stale"):
        log.info("expire_stale_tier3: %s", result)
    return result
