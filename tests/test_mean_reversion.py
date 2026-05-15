import math

import pandas as pd
import pytest

from app.signals.mean_reversion import _rsi


def test_rsi_zero_change_returns_50_ish():
    s = pd.Series([100.0] * 50)
    rsi = _rsi(s, period=2)
    # Wilder RSI on a flat series is undefined initially; after smoothing it
    # should land at the midpoint or above (no losses).
    last = float(rsi.iloc[-1])
    assert not math.isnan(last)
    assert last >= 50  # all-gains-or-flat series should not yield <50


def test_rsi_steep_drop_gives_low_value():
    prices = [100 - i for i in range(20)]  # monotonic decline
    rsi = _rsi(pd.Series(prices), period=2)
    assert float(rsi.iloc[-1]) < 20


def test_rsi_steep_rise_gives_high_value():
    prices = [100 + i for i in range(20)]  # monotonic rise
    rsi = _rsi(pd.Series(prices), period=2)
    assert float(rsi.iloc[-1]) > 80


def test_rsi_oscillation_around_50():
    # Alternating up/down should orbit RSI 50, not push to extremes
    prices = [100 + (1 if i % 2 == 0 else -1) for i in range(30)]
    rsi = _rsi(pd.Series(prices), period=2)
    last = float(rsi.iloc[-1])
    assert 30 < last < 70
