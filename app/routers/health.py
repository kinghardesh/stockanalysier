from fastapi import APIRouter
from sqlalchemy import text

from app.core.db import engine
from app.core.redis import is_kill_switch_active, redis_client

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/deep")
def deep_health() -> dict:
    checks: dict = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        from alpaca.trading.client import TradingClient
        from app.core.config import settings
        client = TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)
        account = client.get_account()
        checks["alpaca"] = {
            "status": "ok",
            "account_status": str(account.status),
            "buying_power": str(account.buying_power),
            "paper": True,
        }
    except Exception as e:
        checks["alpaca"] = f"error: {e}"

    checks["kill_switch_active"] = is_kill_switch_active()
    return checks
