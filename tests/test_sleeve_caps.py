from decimal import Decimal
from unittest.mock import patch

import pytest

from app.risk.sleeve_caps import proposed_sleeve_breach, sleeve_cap_dollars


@pytest.fixture
def allocation():
    return {"trend": 0.50, "discretionary": 0.30, "premium": 0.00, "mean_reversion": 0.15}


def test_discretionary_sleeve_has_budget(allocation):
    """Regression: LLM-driven proposals must not be rejected by the sleeve gate
    just because the discretionary slot was forgotten in settings."""
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        cap = sleeve_cap_dollars("discretionary", Decimal("100000"))
        assert cap == Decimal("30000.00")
        reason = proposed_sleeve_breach(
            "discretionary", Decimal(0), Decimal("5000"), Decimal("100000"),
        )
        assert reason is None


def test_sleeve_cap_dollars_trend(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        assert sleeve_cap_dollars("trend", Decimal("100000")) == Decimal("50000.00")


def test_sleeve_cap_dollars_mean_reversion(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        assert sleeve_cap_dollars("mean_reversion", Decimal("100000")) == Decimal("15000.00")


def test_premium_sleeve_rejected_by_zero_allocation(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        reason = proposed_sleeve_breach(
            "premium", Decimal(0), Decimal("100"), Decimal("100000"),
        )
        assert reason is not None
        assert "zero allocation" in reason


def test_within_cap_returns_none(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        reason = proposed_sleeve_breach(
            "trend", Decimal("10000"), Decimal("5000"), Decimal("100000"),
        )
        assert reason is None


def test_over_cap_returns_reason(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        reason = proposed_sleeve_breach(
            "trend", Decimal("48000"), Decimal("5000"), Decimal("100000"),
        )
        assert reason is not None
        assert "would exceed cap" in reason


def test_unknown_sleeve_treated_as_zero(allocation):
    with patch("app.risk.sleeve_caps.settings") as mock_settings:
        mock_settings.sleeve_allocation = allocation
        reason = proposed_sleeve_breach(
            "fictional", Decimal(0), Decimal("1"), Decimal("100000"),
        )
        assert reason is not None
        assert "zero allocation" in reason
