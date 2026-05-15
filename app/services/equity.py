import logging
from decimal import Decimal

from alpaca.trading.client import TradingClient

from app.core.config import settings
from app.core.redis import redis_client, set_kill_switch

log = logging.getLogger(__name__)

SOD_EQUITY_KEY = "equity:start_of_day"
SOD_TTL_SECONDS = 24 * 60 * 60


class StartOfDayEquityMissing(Exception):
    """Raised when equity:start_of_day is required but absent from Redis."""


def snapshot_sod_equity() -> Decimal:
    client = TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)
    acct = client.get_account()
    equity = Decimal(str(acct.equity))
    redis_client.set(SOD_EQUITY_KEY, str(equity), ex=SOD_TTL_SECONDS)
    log.info("snapshot SOD equity = %s", equity)
    return equity


def read_sod_equity() -> Decimal:
    raw = redis_client.get(SOD_EQUITY_KEY)
    if raw is None:
        log.warning(
            "equity:start_of_day missing — engaging kill switch. "
            "Run scripts/snapshot_equity_now.py then kill_switch.py off to recover."
        )
        set_kill_switch(True)
        raise StartOfDayEquityMissing("equity:start_of_day not set in Redis")
    return Decimal(raw)
