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
    # Target on the wrong side -> clamped to the 5% fallback, then widened by the
    # 1.5:1 min reward:risk rule (stop is 5 below entry, so target >= 107.50).
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("95"), Decimal("90"))
    assert stop == Decimal("95")
    assert target == Decimal("107.50")


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


def test_min_reward_risk_widens_too_close_target():
    # Valid stop 5 below entry, valid target only 2 above -> R:R 0.4. The 1.5:1
    # rule widens the target to entry + 1.5*5 = 107.50.
    stop, target = reconcile_bracket("buy", Decimal("100"), Decimal("95"), Decimal("102"))
    assert stop == Decimal("95")
    assert target == Decimal("107.50")


def test_short_term_horizon_clamps_wide_stop():
    from app.models.enums import TimeHorizon
    # 8.93% proposed stop: kept for unknown horizon (15% band) but clamped to the
    # 3% fallback for an explicit short-term (swing) trade (8% band).
    s_unknown, _ = reconcile_bracket("buy", Decimal("444.73"), Decimal("405"), Decimal("470"))
    assert s_unknown == Decimal("405")
    s_swing, _ = reconcile_bracket("buy", Decimal("444.73"), Decimal("405"), Decimal("470"),
                                   horizon=TimeHorizon.swing)
    assert s_swing == (Decimal("444.73") * Decimal("0.97")).quantize(Decimal("0.01"))


def test_size_position_proposed_cap():
    from app.risk.sizing import size_position
    eq = Decimal("100000")
    # Tight 1% stop -> risk cap allows a huge size, but a 2% proposed cap binds.
    uncapped = size_position(eq, Decimal("100"), Decimal("99"))
    capped = size_position(eq, Decimal("100"), Decimal("99"), max_position_pct=Decimal("0.02"))
    assert capped == 20            # floor(100000 * 0.02 / 100)
    assert capped < uncapped
