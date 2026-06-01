"""Nightly retention job for the market_data hot table.

Exports rows older than `market_data_retention_days` to a gzipped CSV under
`market_data_archive_dir` (the "secondary source"), then deletes them from
Postgres so the live table stays bounded. Archive-then-delete is done per
chunk: if a delete fails, those rows simply get re-archived next run — no loss.
"""
import csv
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import MarketData

log = logging.getLogger(__name__)

_CHUNK = 5000
_COLUMNS = ["id", "ticker", "kind", "event_time", "price", "open", "high",
            "low", "close", "volume", "ingested_at", "raw"]


def _cell(v):
    return "" if v is None else v


def run_once() -> dict:
    result = {"archived": 0, "deleted": 0, "file": None, "errors": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.market_data_retention_days)

    out_dir = os.path.join(settings.market_data_archive_dir, "market_data")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        log.exception("could not create archive dir %s", out_dir)
        result["errors"] += 1
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"market_data_{stamp}.csv.gz")

    f = None
    writer = None
    try:
        while True:
            with SessionLocal() as db:
                rows = db.execute(
                    select(MarketData)
                    .where(MarketData.ingested_at < cutoff)
                    .order_by(MarketData.ingested_at)
                    .limit(_CHUNK)
                ).scalars().all()
                if not rows:
                    break
                if f is None:
                    f = gzip.open(path, "wt", newline="", encoding="utf-8")
                    writer = csv.writer(f)
                    writer.writerow(_COLUMNS)
                ids = []
                for r in rows:
                    ids.append(r.id)
                    writer.writerow([
                        str(r.id), r.ticker, r.kind,
                        r.event_time.isoformat() if r.event_time else "",
                        _cell(r.price), _cell(r.open), _cell(r.high),
                        _cell(r.low), _cell(r.close), _cell(r.volume),
                        r.ingested_at.isoformat() if r.ingested_at else "",
                        json.dumps(r.raw, default=str),
                    ])
                f.flush()
                db.execute(delete(MarketData).where(MarketData.id.in_(ids)))
                db.commit()
                result["archived"] += len(rows)
                result["deleted"] += len(ids)
            if len(rows) < _CHUNK:
                break
    except Exception:
        log.exception("market_data archive failed")
        result["errors"] += 1
    finally:
        if f is not None:
            f.close()
            result["file"] = path

    log.info("archive_market_data: %s", result)
    return result
