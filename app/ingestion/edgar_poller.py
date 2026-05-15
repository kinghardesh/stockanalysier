import asyncio
import json
import logging

import httpx

from app.core.config import settings
from app.core.redis import is_kill_switch_active, redis_client

log = logging.getLogger(__name__)

STREAM_FILINGS = "events:filings"
SEEN_SET = "edgar:seen"
EDGAR_BASE = "https://data.sec.gov"

TICKER_TO_CIK: dict[str, str] = {
    "AAPL": "0000320193", "MSFT": "0000789019", "GOOGL": "0001652044",
    "AMZN": "0001018724", "META": "0001326801", "JPM": "0000019617",
    "V": "0001403161", "JNJ": "0000200406", "PG": "0000080424",
    "KO": "0000021344", "PEP": "0000077476", "WMT": "0000104169",
}

TARGET_FORMS = {"8-K", "4"}
RATE_LIMIT_PER_SEC = 10


class EdgarPoller:
    def __init__(self, tickers: list[str]):
        self.tickers = [t for t in tickers if t in TICKER_TO_CIK]
        self._semaphore = asyncio.Semaphore(RATE_LIMIT_PER_SEC)

    async def _throttled_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        async with self._semaphore:
            resp = await client.get(url, timeout=20)
            await asyncio.sleep(1.0 / RATE_LIMIT_PER_SEC)
            return resp

    async def _fetch_submissions(self, client: httpx.AsyncClient, ticker: str) -> list[dict]:
        cik = TICKER_TO_CIK[ticker]
        url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
        resp = await self._throttled_get(client, url)
        if resp.status_code != 200:
            log.warning("edgar %s %s -> %s", ticker, url, resp.status_code)
            return []
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {}) or {}
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filed_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        out = []
        for form, acc, filed, doc in zip(forms, accessions, filed_dates, primary_docs):
            if form not in TARGET_FORMS:
                continue
            acc_clean = acc.replace("-", "")
            url_doc = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc}"
            out.append({
                "ticker": ticker, "cik": cik, "form_type": form,
                "accession": acc, "filed_at": filed, "url": url_doc,
            })
        return out

    async def poll_once(self) -> int:
        if is_kill_switch_active():
            log.info("kill switch active; edgar poll skipped")
            return 0

        emitted = 0
        headers = {"User-Agent": settings.edgar_user_agent, "Accept": "application/json"}
        async with httpx.AsyncClient(headers=headers) as client:
            for ticker in self.tickers:
                try:
                    items = await self._fetch_submissions(client, ticker)
                except Exception:
                    log.exception("edgar fetch failed for %s", ticker)
                    continue
                for it in items:
                    fp = f"{it['cik']}:{it['accession']}"
                    if redis_client.sismember(SEEN_SET, fp):
                        continue
                    redis_client.sadd(SEEN_SET, fp)
                    redis_client.xadd(STREAM_FILINGS, {"data": json.dumps(it)},
                                      maxlen=50_000, approximate=True)
                    emitted += 1
        return emitted
