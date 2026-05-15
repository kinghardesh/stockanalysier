import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import is_kill_switch_active, redis_client
from app.ingestion.sanitizer import sanitize
from app.models import SanitizationLog

log = logging.getLogger(__name__)

STREAM_NEWS = "events:news"
SEEN_SET = "news:seen"
NEWSAPI_URL = "https://newsapi.org/v2/everything"


class NewsPoller:
    def __init__(self, tickers: list[str], api_key: str | None = None):
        self.tickers = tickers
        self.api_key = api_key or settings.newsapi_key

    @staticmethod
    def _fingerprint(title: str, source: str) -> str:
        return hashlib.sha256(f"{title}|{source}".encode("utf-8")).hexdigest()

    async def _fetch_for(self, client: httpx.AsyncClient, ticker: str) -> list[dict]:
        params = {
            "q": ticker,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 25,
            "apiKey": self.api_key,
        }
        r = await client.get(NEWSAPI_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("articles", []) or []

    async def poll_once(self) -> int:
        if not self.api_key:
            log.warning("NEWSAPI_KEY not set; skipping news poll")
            return 0
        if is_kill_switch_active():
            log.info("kill switch active; news poll skipped")
            return 0

        emitted = 0
        async with httpx.AsyncClient() as client:
            for ticker in self.tickers:
                try:
                    articles = await self._fetch_for(client, ticker)
                except Exception:
                    log.exception("news fetch failed for %s", ticker)
                    continue
                for art in articles:
                    title = art.get("title") or ""
                    source = (art.get("source") or {}).get("name") or ""
                    fp = self._fingerprint(title, source)
                    if redis_client.sismember(SEEN_SET, fp):
                        continue
                    redis_client.sadd(SEEN_SET, fp)
                    text = f"{title}. {art.get('description') or ''}"
                    result = sanitize(text, [ticker])
                    self._log_sanitization(ticker, art.get("url"), text, result)
                    payload = {
                        "ticker": ticker,
                        "title_sanitized": result.sanitized,
                        "source": source,
                        "url": art.get("url") or "",
                        "published_at": art.get("publishedAt") or datetime.now(timezone.utc).isoformat(),
                    }
                    redis_client.xadd(STREAM_NEWS, {"data": json.dumps(payload)},
                                      maxlen=50_000, approximate=True)
                    emitted += 1
        return emitted

    def _log_sanitization(self, ticker, url, original, result) -> None:
        if not result.stripped:
            return
        with SessionLocal() as db:
            try:
                db.add(SanitizationLog(
                    source="newsapi",
                    source_ref=(url or "")[:256],
                    original_excerpt=original[:4000],
                    stripped_fragments=result.stripped,
                    sanitized_text=result.sanitized,
                ))
                db.commit()
            except Exception:
                log.exception("failed to log sanitization for %s", ticker)
                db.rollback()
