import redis

from app.core.config import settings

KILL_SWITCH_KEY = "system:kill_switch"

redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def is_kill_switch_active() -> bool:
    return redis_client.get(KILL_SWITCH_KEY) == "true"


def set_kill_switch(active: bool, ttl_seconds: int | None = None) -> None:
    if active:
        if ttl_seconds:
            redis_client.set(KILL_SWITCH_KEY, "true", ex=ttl_seconds)
        else:
            redis_client.set(KILL_SWITCH_KEY, "true")
    else:
        redis_client.delete(KILL_SWITCH_KEY)
