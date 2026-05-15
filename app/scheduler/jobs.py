import logging
from datetime import date
from functools import wraps
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from app.core.redis import is_kill_switch_active

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    sched = _NYSE.valid_days(start_date=d.isoformat(), end_date=d.isoformat())
    return len(sched) > 0


def with_kill_switch(name: str):
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if is_kill_switch_active():
                log.warning("kill switch ON; skipping job %s", name)
                return None
            return await fn(*args, **kwargs)
        return wrapper
    return deco


def trading_day_only(name: str):
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if not is_trading_day():
                log.info("not a trading day; skipping %s", name)
                return None
            return await fn(*args, **kwargs)
        return wrapper
    return deco
