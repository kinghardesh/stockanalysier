import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import is_kill_switch_active, redis_client
from app.core.whitelist import TICKER_ALIASES
from app.ingestion.sanitizer import sanitize
from app.models import SanitizationLog

log = logging.getLogger(__name__)

STREAM_NEWS = "events:news"
SEEN_SET = "news:seen"
NEWSAPI_URL = "https://newsapi.org/v2/everything"


class NewsPoller:
    """Polls NewsAPI for whitelist company news in a single batched request.

    The free tier allows 100 requests/day. Querying per-ticker (12 requests
    per cycle) blows that budget within ~2 hours. Instead we build ONE OR
    query from company-name aliases per cycle, then attribute each returned
    article back to ticker(s) by matching aliases in the headline/description.
    """

    def __init__(self, tickers: list[str], api_key: str | None = None):
        self.tickers = tickers
        self.api_key = api_key or settings.newsapi_key

    @staticmethod
    def _fingerprint(title: str, source: str) -> str:
        return hashlib.sha256(f"{title}|{source}".encode("utf-8")).hexdigest()

    def _build_query(self) -> str:
        terms: list[str] = []
        for ticker in self.tickers:
            for alias in TICKER_ALIASES.get(ticker, [ticker]):
                terms.append(f'"{alias}"')
        return " OR ".join(terms)

    def _match_tickers(self, text: str) -> list[str]:
        text_lower = text.lower()
        matched: list[str] = []
        for ticker in self.tickers:
            aliases = TICKER_ALIASES.get(ticker, [ticker])
            if any(alias.lower() in text_lower for alias in aliases):
                matched.append(ticker)
        return matched

    async def _fetch_batch(self, client: httpx.AsyncClient) -> list[dict]:
        params = {
            "q": self._build_query(),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 100,
            "apiKey": self.api_key,
        }
        r = await client.get(NEWSAPI_URL, params=params, timeout=20)
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
            try:
                articles = await self._fetch_batch(client)
            except httpx.HTTPStatusError as e:
                if e.response is not None and e.response.status_code == 429:
                    log.warning("NewsAPI rate limit (429) — daily quota exhausted")
                else:
                    log.exception("news batch fetch failed")
                return 0
            except Exception:
                log.exception("news batch fetch failed")
                return 0

            for art in articles:
                title = art.get("title") or ""
                description = art.get("description") or ""
                source = (art.get("source") or {}).get("name") or ""
                haystack = f"{title} {description}"
                matched = self._match_tickers(haystack)
                if not matched:
                    continue

                fp = self._fingerprint(title, source)
                if redis_client.sismember(SEEN_SET, fp):
                    continue
                redis_client.sadd(SEEN_SET, fp)

                text = f"{title}. {description}"
                for ticker in matched:
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

        log.info("news_poll: fetched %d articles, emitted %d ticker-events",
                 len(articles), emitted)
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
