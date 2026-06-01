"""Company fundamentals snapshot -> permanent company_fundamentals table.

Built on reliable sources only (no screen-scraping):
  - sector: the local SECTOR_MAP (always available).
  - 52-week high/low: computed from our own daily_bars (Alpaca-sourced).
  - market cap / PE / EPS / beta / dividend / industry / name: Finnhub REST API,
    used ONLY when `finnhub_api_key` is configured (free tier, datacenter-safe).

With no Finnhub key the job still populates a useful row (sector + 52-week range).
Everything is best-effort and per-ticker isolated: one failure never aborts the run.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.models import CompanyFundamentals, DailyBar

log = logging.getLogger(__name__)

_FINNHUB = "https://finnhub.io/api/v1"


def _dec(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clip(v, n):
    return str(v)[:n] if v else None


def _fiftytwo_week_from_bars(db, symbol: str):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date()
    hi, lo = db.execute(
        select(func.max(DailyBar.high), func.min(DailyBar.low))
        .where(DailyBar.ticker == symbol.upper(), DailyBar.bar_date >= cutoff)
    ).one()
    return hi, lo


def _finnhub(symbol: str, key: str) -> dict:
    """Return {profile, metric} dicts from Finnhub, or {} on any failure."""
    try:
        with httpx.Client(timeout=10.0) as client:
            profile = client.get(f"{_FINNHUB}/stock/profile2",
                                  params={"symbol": symbol, "token": key}).json() or {}
            metric = client.get(f"{_FINNHUB}/stock/metric",
                                params={"symbol": symbol, "metric": "all", "token": key}).json() or {}
        return {"profile": profile, "metric": metric.get("metric", {}) or {}}
    except Exception:
        log.exception("finnhub fetch failed for %s", symbol)
        return {}


def refresh_fundamentals(tickers=None) -> dict:
    if not settings.fundamentals_refresh_enabled:
        return {"skipped": True}

    tickers = list(tickers or WHITELIST)
    key = settings.finnhub_api_key.strip()
    result = {"tickers": len(tickers), "updated": 0, "with_finnhub": 0, "errors": 0}

    for symbol in tickers:
        sym = symbol.upper()
        vals = dict(
            ticker=sym,
            sector=_clip(SECTOR_MAP.get(sym), 64),
            name=None, industry=None, market_cap=None, pe_ratio=None,
            forward_pe=None, eps=None, dividend_yield=None, beta=None,
            fifty_two_week_high=None, fifty_two_week_low=None,
            next_earnings_date=None, raw={},
        )

        try:
            with SessionLocal() as db:
                hi, lo = _fiftytwo_week_from_bars(db, sym)
                vals["fifty_two_week_high"] = hi
                vals["fifty_two_week_low"] = lo

                if key:
                    fh = _finnhub(sym, key)
                    if fh:
                        prof, met = fh.get("profile", {}), fh.get("metric", {})
                        if prof or met:
                            result["with_finnhub"] += 1
                        vals["name"] = _clip(prof.get("name"), 128)
                        vals["industry"] = _clip(prof.get("finnhubIndustry"), 128)
                        mc = prof.get("marketCapitalization")  # Finnhub: in millions
                        vals["market_cap"] = _dec(float(mc) * 1_000_000) if mc else None
                        vals["pe_ratio"] = _dec(met.get("peNormalizedAnnual") or met.get("peTTM"))
                        vals["forward_pe"] = _dec(met.get("forwardPE"))
                        vals["eps"] = _dec(met.get("epsTTM") or met.get("epsBasicExclExtraItemsTTM"))
                        vals["beta"] = _dec(met.get("beta"))
                        dy = met.get("dividendYieldIndicatedAnnual")
                        vals["dividend_yield"] = _dec(float(dy) / 100.0) if dy else None
                        if met.get("52WeekHigh"):
                            vals["fifty_two_week_high"] = _dec(met.get("52WeekHigh"))
                        if met.get("52WeekLow"):
                            vals["fifty_two_week_low"] = _dec(met.get("52WeekLow"))
                        vals["raw"] = {k: v for k, v in {**prof, **met}.items()
                                       if isinstance(v, (str, int, float, bool)) or v is None}

                stmt = pg_insert(CompanyFundamentals).values(**vals)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ticker"],
                    set_={**{k: stmt.excluded[k] for k in vals if k != "ticker"},
                          "updated_at": func.now()},
                )
                db.execute(stmt)
                db.commit()
            result["updated"] += 1
        except Exception:
            log.exception("fundamentals upsert failed for %s", sym)
            result["errors"] += 1

    log.info("refresh_fundamentals: %s", result)
    return result
