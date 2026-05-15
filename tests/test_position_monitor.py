from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.models import Position
from app.models.enums import TradeSleeve
from app.signals.position_monitor import PositionMonitor


def _pos(qty="10", entry="100", stop=None, target=None, ticker="AAPL"):
    p = Position(
        ticker=ticker,
        qty=Decimal(qty),
        avg_entry_price=Decimal(entry),
        current_stop=Decimal(stop) if stop else None,
        current_target=Decimal(target) if target else None,
        sleeve=TradeSleeve.trend,
    )
    return p


def test_long_stop_hit_when_price_below_stop():
    p = _pos(qty="10", entry="100", stop="95")
    assert PositionMonitor._evaluate(p, Decimal("94.99")) == "stop"


def test_long_stop_not_hit_above_stop():
    p = _pos(qty="10", entry="100", stop="95")
    assert PositionMonitor._evaluate(p, Decimal("96")) is None


def test_long_target_hit_when_price_above_target():
    p = _pos(qty="10", entry="100", stop="95", target="110")
    assert PositionMonitor._evaluate(p, Decimal("111")) == "target"


def test_short_stop_hit_when_price_above_stop():
    p = _pos(qty="-10", entry="100", stop="105")
    assert PositionMonitor._evaluate(p, Decimal("106")) == "stop"


def test_no_stop_no_target_no_action():
    p = _pos(qty="10", entry="100")
    assert PositionMonitor._evaluate(p, Decimal("50")) is None


def test_zero_qty_no_action():
    p = _pos(qty="0", entry="100", stop="95")
    assert PositionMonitor._evaluate(p, Decimal("90")) is None


def test_close_invoked_on_stop_hit(monkeypatch):
    import asyncio

    mock_executor = MagicMock()

    async def fake_close(ticker):
        fake_close.called_with = ticker
    fake_close.called_with = None
    mock_executor.close_position = fake_close

    pos = _pos(ticker="MSFT", qty="10", entry="100", stop="95")
    monitor = PositionMonitor(executor=mock_executor)

    asyncio.run(monitor._close(pos, Decimal("94"), reason="stop"))
    assert fake_close.called_with == "MSFT"
