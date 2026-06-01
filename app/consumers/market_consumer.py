"""Persist live market events into Postgres.

Drains the `events:market` Redis stream (populated by AlpacaMarketStream with
bars + trades) via a consumer group and writes rows into the `market_data`
hot table. These rows are kept for `market_data_retention_days`, after which
the nightly archive job offloads them to a compressed secondary store.

Bars and trades can each be disabled independently via the persist_market_*
settings (trades are far higher volume than bars).
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import redis_client
from app.models import MarketData

log = logging.getLogger(__name__)

STREAM_MARKET = "events:market"
CONSUMER_GROUP = "market_persist"
CONSUMER_NAME = f"market_consumer_{uuid4().hex[:8]}"


def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def _parse_time(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


class MarketDataConsumer:
    def __init__(self):
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            # id="$" => only collect events that arrive after the group exists,
            # so we don't replay the whole capped backlog on first start.
            redis_client.xgroup_create(STREAM_MARKET, CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.exception("xgroup_create failed for %s", STREAM_MARKET)

    def run_once(self) -> int:
        batch = redis_client.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {STREAM_MARKET: ">"},
            count=settings.market_data_batch_size,
            block=2000,
        )
        if not batch:
            return 0

        rows: list[MarketData] = []
        msg_ids: list[str] = []
        for _, messages in batch:
            for msg_id, fields in messages:
                msg_ids.append(msg_id)
                kind = fields.get("kind", "") or "unknown"
                if kind == "bar" and not settings.persist_market_bars:
                    continue
                if kind == "trade" and not settings.persist_market_trades:
                    continue
                try:
                    data = json.loads(fields.get("data", "{}"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                ticker = data.get("symbol") or data.get("S") or data.get("ticker")
                if not ticker:
                    continue
                rows.append(MarketData(
                    ticker=str(ticker).upper(),
                    kind=kind,
                    event_time=_parse_time(data.get("timestamp") or data.get("t")),
                    price=_dec(data.get("price") if data.get("price") is not None else data.get("p")),
                    open=_dec(data.get("open") if data.get("open") is not None else data.get("o")),
                    high=_dec(data.get("high") if data.get("high") is not None else data.get("h")),
                    low=_dec(data.get("low") if data.get("low") is not None else data.get("l")),
                    close=_dec(data.get("close") if data.get("close") is not None else data.get("c")),
                    volume=_int(data.get("volume") if data.get("volume") is not None
                                else (data.get("size") if data.get("size") is not None else data.get("v"))),
                    raw=data,
                ))

        if rows:
            try:
                with SessionLocal() as db:
                    db.add_all(rows)
                    db.commit()
            except Exception:
                log.exception("failed to persist %d market_data rows", len(rows))

        if msg_ids:
            redis_client.xack(STREAM_MARKET, CONSUMER_GROUP, *msg_ids)
        return len(rows)
