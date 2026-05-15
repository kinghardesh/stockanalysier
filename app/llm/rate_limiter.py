import logging
from datetime import datetime, timezone

from app.core.redis import redis_client

log = logging.getLogger(__name__)


PROVIDER_LIMITS: dict[str, dict[str, int | None]] = {
    "gemini":     {"per_minute": 10, "per_day": 1500},
    "ollama":     {"per_minute": None, "per_day": None},
    "openrouter": {"per_minute": 20, "per_day": 50},
}

MINUTE_TTL = 90
DAY_TTL = 25 * 60 * 60


class RedisRateLimiter:
    def __init__(self, limits: dict | None = None, client=None):
        self.limits = limits or PROVIDER_LIMITS
        self.client = client or redis_client

    async def allow(self, provider_name: str) -> bool:
        limits = self.limits.get(provider_name)
        if not limits or (limits.get("per_minute") is None and limits.get("per_day") is None):
            return True

        now = datetime.now(timezone.utc)
        minute_key = f"ratelimit:{provider_name}:minute:{now.strftime('%Y%m%d%H%M')}"
        day_key = f"ratelimit:{provider_name}:day:{now.strftime('%Y%m%d')}"

        pipe = self.client.pipeline()
        pipe.incr(minute_key)
        pipe.expire(minute_key, MINUTE_TTL)
        pipe.incr(day_key)
        pipe.expire(day_key, DAY_TTL)
        minute_count, _, day_count, _ = pipe.execute()

        per_min = limits.get("per_minute")
        per_day = limits.get("per_day")

        if per_min is not None and minute_count > per_min:
            log.warning("rate limit %s: %d/min > %d", provider_name, minute_count, per_min)
            return False
        if per_day is not None and day_count > per_day:
            log.warning("rate limit %s: %d/day > %d", provider_name, day_count, per_day)
            return False
        return True
