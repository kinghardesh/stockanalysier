from decimal import Decimal

from app.risk.sizing import reconcile_bracket


def test_buy_valid_levels_kept():
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("95"), Decimal("110"))
    assert stop == Decimal("95")
    assert target == Decimal("110")


def test_buy_stop_above_entry_clamped():
    # LLM proposed stop ABOVE entry (invalid for a buy) -> clamp to 3% below
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("105"), Decimal("110"))
    assert stop == Decimal("97.00")
    assert target == Decimal("110")


def test_buy_target_below_entry_clamped():
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("95"), Decimal("90"))
    assert stop == Decimal("95")
    assert target == Decimal("105.00")


def test_buy_both_wrong_side_both_clamped():
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("110"), Decimal("90"))
    assert stop == Decimal("97.00")
    assert target == Decimal("105.00")


def test_sell_valid_levels_kept():
    stop, target = reconcile_bracket("sell", Decimal("100"), Decimal("105"), Decimal("90"))
    assert stop == Decimal("105")
    assert target == Decimal("90")


def test_sell_stop_below_entry_clamped():
    # For a sell, stop must be ABOVE entry; LLM put it below -> clamp 3% above
    stop, target = reconcile_bracket("sell", Decimal("100"), Decimal("95"), Decimal("90"))
    assert stop == Decimal("103.00")
    assert target == Decimal("90")


def test_buy_stale_prices_clamped_to_live_entry():
    # The real bug: AAPL at $310 live, but Gemini proposed $170 stop / $185
    # target from stale training data. Both are absurd distances from entry.
    stop, target = reconcile_bracket("buy", Decimal("310"), Decimal("170"), Decimal("185"))
    assert stop == Decimal("300.70")    # 310 * 0.97, NOT the stale 170
    assert target == Decimal("325.50")  # 310 * 1.05, NOT the stale 185
    assert stop < Decimal("310") < target


def test_buy_stop_too_far_below_clamped():
    # stop directionally correct (below entry) but 20% away -> clamp to 3%
    stop, _ = reconcile_bracket("buy", Decimal("100"), Decimal("80"), Decimal("105"))
    assert stop == Decimal("97.00")


def test_none_levels_get_defaults():
    stop, target = reconcile_bracket("buy", Decimal("200"), None, None)
    assert stop == Decimal("194.00")   # 200 * 0.97
    assert target == Decimal("210.00")  # 200 * 1.05


def test_result_always_brackets_entry_for_buy():
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("999"), Decimal("1"))
    assert stop < Decimal("100") < target


def test_result_always_brackets_entry_for_sell():
    stop, target = reconcile_bracket("sell", Decimal("100"), Decimal("1"), Decimal("999"))
    assert target < Decimal("100") < stop
