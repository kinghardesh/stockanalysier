"""Standalone GPT verdict helper (the second-opinion verifier).

Used by the Tier-3 timeout fallback approver. Returns a VerifierVerdict, or
None when OpenAI is not configured / unavailable (callers must treat None as
"no opinion" and fall back to their default behavior).
"""
import json
import logging
from typing import Optional

from app.core.config import settings
from app.llm.prompts import VERIFIER_PROMPT_TEMPLATE
from app.llm.schemas import VerifierVerdict, verifier_schema

log = logging.getLogger(__name__)


def _build_verifier():
    if not settings.openai_api_key:
        return None
    try:
        from app.llm.providers.openai_provider import OpenAIProvider
        return OpenAIProvider()
    except Exception as e:
        log.warning("verifier init failed: %s", e)
        return None


async def gpt_verdict(
    *, ticker, side, confidence, proposed_size_pct, stop_price, target_price,
    time_horizon, thesis, invalidation_criteria="",
) -> Optional[VerifierVerdict]:
    prov = _build_verifier()
    if prov is None:
        return None
    schema = verifier_schema()
    prompt = VERIFIER_PROMPT_TEMPLATE.format(
        ticker=ticker, side=side, confidence=confidence,
        proposed_size_pct=proposed_size_pct, stop_price=stop_price,
        target_price=target_price, time_horizon=time_horizon, thesis=thesis,
        invalidation_criteria=invalidation_criteria or "(not provided)",
        schema_json=json.dumps(schema),
    )
    try:
        raw = await prov.generate_structured(
            prompt, schema, timeout=settings.gpt_verifier_timeout_seconds)
        return VerifierVerdict(**raw)
    except Exception as e:
        log.warning("gpt_verdict failed for %s: %s", ticker, e)
        return None
