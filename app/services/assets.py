"""Sync the full Alpaca-tradable universe into the `assets` table.

One API call returns every active US equity (~13k); we upsert them in chunks.
Cheap reference data — what exists, where it trades, and its trade flags.
"""
import logging

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import SessionLocal
from app.execution.service import _alpaca
from app.models import Asset

log = logging.getLogger(__name__)

_FIELDS = ("name", "exchange", "asset_class", "status", "tradable",
           "marginable", "shortable", "easy_to_borrow", "fractionable")


def _enum_str(v):
    if v is None:
        return None
    return str(v).split(".")[-1]


def sync_assets() -> dict:
    client = _alpaca()
    try:
        assets = client.get_all_assets(GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
    except Exception:
        log.exception("sync_assets: get_all_assets failed")
        return {"error": "fetch failed"}

    rows = []
    for a in assets:
        rows.append(dict(
            symbol=a.symbol,
            name=(a.name or "")[:256] or None,
            exchange=_enum_str(a.exchange),
            asset_class=_enum_str(a.asset_class),
            status=_enum_str(a.status),
            tradable=bool(a.tradable),
            marginable=bool(a.marginable),
            shortable=bool(a.shortable),
            easy_to_borrow=bool(a.easy_to_borrow),
            fractionable=bool(a.fractionable),
        ))

    if not rows:
        return {"synced": 0}

    stmt = pg_insert(Asset)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={**{f: stmt.excluded[f] for f in _FIELDS}, "updated_at": func.now()},
    )
    synced = 0
    try:
        with SessionLocal() as db:
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                db.execute(stmt, chunk)
                synced += len(chunk)
            db.commit()
    except Exception:
        log.exception("sync_assets: upsert failed")
        return {"error": "upsert failed", "fetched": len(rows)}

    result = {"synced": synced, "tradable": sum(1 for r in rows if r["tradable"])}
    log.info("sync_assets: %s", result)
    return result
