"""Regression tests for the start-of-day equity baseline.

This is the load-bearing piece of the daily-loss circuit breaker: if
equity:start_of_day is missing or wrong, the 3%-loss kill switch can't fire.

Uses fakeredis to avoid requiring docker compose. If fakeredis isn't
installed, the tests are skipped with a clear message rather than silently
falling through to real Redis (which would mask local bugs).
"""
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

fakeredis = pytest.importorskip(
    "fakeredis",
    reason="fakeredis required — pip install fakeredis or run inside docker compose",
)

from app.core.redis import KILL_SWITCH_KEY
from app.services import equity as equity_module
from app.services.equity import (
    SOD_EQUITY_KEY, SOD_TTL_SECONDS, StartOfDayEquityMissing,
    read_sod_equity, snapshot_sod_equity,
)


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace the live Redis client wherever it has been imported.

    `redis_client` is a module-level reference in both app.core.redis and
    app.services.equity (the latter imported the name at module load). Both
    bindings need patching for set_kill_switch and the equity helpers to use
    the fake.
    """
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.core.redis.redis_client", fake)
    monkeypatch.setattr("app.services.equity.redis_client", fake)
    yield fake


def _mock_trading_client(equity_value: str, monkeypatch):
    """Patch the TradingClient inside equity.py to avoid touching Alpaca."""
    mock_account = MagicMock()
    mock_account.equity = equity_value
    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    mock_factory = MagicMock(return_value=mock_client)
    monkeypatch.setattr(equity_module, "TradingClient", mock_factory)
    return mock_factory


# --- (a) snapshot_sod_equity writes the right value with 24h TTL ----------

def test_snapshot_writes_value_with_24h_ttl(fake_redis, monkeypatch):
    _mock_trading_client("123456.78", monkeypatch)

    result = snapshot_sod_equity()

    assert result == Decimal("123456.78")
    assert fake_redis.get(SOD_EQUITY_KEY) == "123456.78"

    ttl = fake_redis.ttl(SOD_EQUITY_KEY)
    # 24h window with a 5s tolerance for test-execution lag.
    assert 86_395 <= ttl <= 86_400, f"expected ~86400s TTL, got {ttl}"


# --- (b) read_sod_equity returns the persisted baseline -------------------

def test_read_sod_equity_returns_stored_value(fake_redis):
    fake_redis.set(SOD_EQUITY_KEY, "100000.00", ex=SOD_TTL_SECONDS)
    assert read_sod_equity() == Decimal("100000.00")


def test_read_sod_equity_returns_decimal_not_string(fake_redis):
    fake_redis.set(SOD_EQUITY_KEY, "99750.55", ex=SOD_TTL_SECONDS)
    result = read_sod_equity()
    assert isinstance(result, Decimal)
    # Confirm arithmetic works (regression: would fail if str leaked through)
    assert result - Decimal("100") == Decimal("99650.55")


# --- (c) missing key engages kill switch + refuses to validate -----------

def test_missing_sod_engages_kill_switch_with_no_ttl(fake_redis):
    assert fake_redis.get(SOD_EQUITY_KEY) is None
    assert fake_redis.get(KILL_SWITCH_KEY) is None

    with pytest.raises(StartOfDayEquityMissing):
        read_sod_equity()

    assert fake_redis.get(KILL_SWITCH_KEY) == "true"
    # No TTL: requires manual reset (Phase 2.5 contract).
    assert fake_redis.ttl(KILL_SWITCH_KEY) == -1


def test_engine_refuses_validation_when_sod_missing(fake_redis):
    """The risk engine reads starting_equity_today from the AccountState the
    caller assembles. The caller (_alpaca_account_state) routes through
    read_sod_equity, which raises if the key is missing. That exception
    propagates and prevents the engine from being called at all — the
    'refuse to validate' contract is enforced at AccountState construction.
    """
    assert fake_redis.get(SOD_EQUITY_KEY) is None

    # Simulate the caller's path: try to build state -> raise.
    with pytest.raises(StartOfDayEquityMissing):
        read_sod_equity()

    # Kill switch is now engaged; any subsequent scheduled job would see
    # is_kill_switch_active() == True and skip via the @with_kill_switch
    # decorator. We verify the engagement here as the regression invariant.
    assert fake_redis.get(KILL_SWITCH_KEY) == "true"


# --- (d) snapshot-equity-now CLI writes the same key ----------------------

def test_cli_snapshot_writes_same_key(fake_redis, monkeypatch):
    _mock_trading_client("200000.00", monkeypatch)

    from scripts.snapshot_equity_now import main

    rc = main()

    assert rc == 0
    assert fake_redis.get(SOD_EQUITY_KEY) == "200000.00"
    ttl = fake_redis.ttl(SOD_EQUITY_KEY)
    assert SOD_TTL_SECONDS - 5 <= ttl <= SOD_TTL_SECONDS
