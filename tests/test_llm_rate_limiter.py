import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.redis import redis_client
from app.llm.rate_limiter import RedisRateLimiter


@pytest.fixture(autouse=True)
def _clear_keys():
    for k in redis_client.keys("ratelimit:test_*"):
        redis_client.delete(k)
    yield
    for k in redis_client.keys("ratelimit:test_*"):
        redis_client.delete(k)


def test_under_minute_limit_returns_true():
    limiter = RedisRateLimiter(limits={"test_x": {"per_minute": 3, "per_day": 100}})
    assert [asyncio.run(limiter.allow("test_x")) for _ in range(3)] == [True, True, True]


def test_at_minute_limit_returns_false():
    limiter = RedisRateLimiter(limits={"test_x": {"per_minute": 3, "per_day": 100}})
    for _ in range(3):
        asyncio.run(limiter.allow("test_x"))
    assert asyncio.run(limiter.allow("test_x")) is False


def test_no_limits_always_true():
    limiter = RedisRateLimiter(limits={"test_local": {"per_minute": None, "per_day": None}})
    assert all(asyncio.run(limiter.allow("test_local")) for _ in range(20))


def test_day_rollover_resets():
    limiter = RedisRateLimiter(limits={"test_y": {"per_minute": 100, "per_day": 2}})
    day1 = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    day2 = day1 + timedelta(days=1)
    with patch("app.llm.rate_limiter.datetime") as mdt:
        mdt.now.return_value = day1
        assert asyncio.run(limiter.allow("test_y")) is True
        assert asyncio.run(limiter.allow("test_y")) is True
        assert asyncio.run(limiter.allow("test_y")) is False
    with patch("app.llm.rate_limiter.datetime") as mdt:
        mdt.now.return_value = day2
        assert asyncio.run(limiter.allow("test_y")) is True
