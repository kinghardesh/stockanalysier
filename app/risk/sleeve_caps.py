"""Per-sleeve capital allocation enforcement.

The risk engine consults this module to translate a sleeve allocation policy
(settings.sleeve_allocation, e.g. {"trend": 0.50, "mean_reversion": 0.15, "premium": 0.00})
into hard $ caps against current AccountState.equity and verify that adding a
new proposal would not exceed the sleeve's slice.

Premium currently allocates 0.00 → any premium proposal is rejected at this gate.
"""
from decimal import Decimal
from typing import Optional

from app.core.config import settings


def sleeve_cap_dollars(sleeve: str, equity: Decimal) -> Decimal:
    """Return the $ cap for a given sleeve at the supplied equity level."""
    fraction = settings.sleeve_allocation.get(sleeve, 0.0)
    return equity * Decimal(str(fraction))


def proposed_sleeve_breach(
    sleeve: str, current_sleeve_exposure: Decimal, new_dollar_exposure: Decimal, equity: Decimal,
) -> Optional[str]:
    """Return a human-readable rejection reason if adding new_dollar_exposure
    would push the sleeve past its cap. Returns None when within cap.
    """
    cap = sleeve_cap_dollars(sleeve, equity)
    if cap <= 0:
        return (
            f"sleeve {sleeve!r} has zero allocation (cap=${cap:.2f}); "
            f"proposal rejected at the sleeve gate"
        )
    new_total = current_sleeve_exposure + new_dollar_exposure
    if new_total > cap:
        return (
            f"sleeve {sleeve!r} would exceed cap: current=${current_sleeve_exposure:.2f} + "
            f"proposed=${new_dollar_exposure:.2f} > cap=${cap:.2f}"
        )
    return None
