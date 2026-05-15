import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass

from alpaca.data.live import StockDataStream

from app.core.config import settings
from app.core.redis import is_kill_switch_active, redis_client

log = logging.getLogger(__name__)

STREAM_MARKET = "events:market"
MAX_STREAM_LEN = 100_000


def _encode(payload) -> dict:
    if is_dataclass(payload):
        d = asdict(payload)
    elif hasattr(payload, "model_dump"):
        d = payload.model_dump()
    elif hasattr(payload, "_raw"):
        d = dict(payload._raw)
    else:
        d = {"repr": repr(payload)}
    return {"data": json.dumps(d, default=str)}


class AlpacaMarketStream:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._stop = asyncio.Event()

    async def _on_bar(self, bar):
        try:
            redis_client.xadd(STREAM_MARKET, _encode(bar) | {"kind": "bar"},
                              maxlen=MAX_STREAM_LEN, approximate=True)
        except Exception:
            log.exception("failed to push bar")

    async def _on_trade(self, trade):
        try:
            redis_client.xadd(STREAM_MARKET, _encode(trade) | {"kind": "trade"},
                              maxlen=MAX_STREAM_LEN, approximate=True)
        except Exception:
            log.exception("failed to push trade")

    async def run_forever(self):
        backoff = 1
        while not self._stop.is_set():
            if is_kill_switch_active():
                log.warning("kill switch active; market stream sleeping")
                await asyncio.sleep(30)
                continue
            try:
                client = StockDataStream(settings.alpaca_api_key, settings.alpaca_api_secret)
                client.subscribe_bars(self._on_bar, *self.symbols)
                client.subscribe_trades(self._on_trade, *self.symbols)
                log.info("alpaca stream connected for %s", self.symbols)
                await asyncio.to_thread(client.run)
                backoff = 1
            except Exception:
                log.exception("alpaca stream error; backing off %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self):
        self._stop.set()
