import pytest

from guardrails import (
    AccountSnapshot,
    Guardrails,
    Limits,
    LimitsError,
    OrderIntent,
    Position,
    Side,
    find_unmanaged_positions,
)


def account(**kw):
    base = {"equity": 10_000, "cash": 10_000, "day_start_equity": 10_000, "positions": {}, "trades_today": 0}
    base.update(kw)
    return AccountSnapshot(**base)


def buy(qty=10, price=100.0, stop=90.0, symbol="VRT"):
    return OrderIntent(symbol, Side.BUY, qty, price, stop)


def test_normal_entry_allowed():
    assert Guardrails().check(buy(), account())


def test_entry_without_stop_blocked():
    d = Guardrails().check(buy(stop=None), account())
    assert not d and "no stop-loss" in d.reasons[0]


def test_stop_too_wide_blocked():
    d = Guardrails(Limits(max_stop_loss_pct=10)).check(buy(stop=85), account())
    assert not d and any("wider" in r for r in d.reasons)


def test_position_size_cap():
    # 30 x $100 = $3,000 = 30% of equity, over the default 25%
    d = Guardrails().check(buy(qty=30), account())
    assert not d and any("of equity" in r for r in d.reasons)


def test_position_cap_counts_existing_holding():
    held = {"VRT": Position("VRT", 20, 2_000, 100)}
    d = Guardrails().check(buy(qty=10), account(positions=held, cash=8_000))
    assert not d  # 2,000 + 1,000 = 30%


def test_daily_loss_stops_new_entries():
    d = Guardrails().check(buy(), account(equity=9_600, cash=9_600))  # -4% today
    assert not d and any("today" in r for r in d.reasons)


def test_trade_count_cap():
    assert not Guardrails().check(buy(), account(trades_today=10))


def test_kill_switch_blocks_entries_but_not_exits():
    g = Guardrails(kill_switch=True)
    held = {"VRT": Position("VRT", 36, 8_000, 315)}
    assert not g.check(buy(), account())
    sell = OrderIntent("VRT", Side.SELL, 36, 224, None)
    assert g.check(sell, account(positions=held))


def test_no_margin():
    assert not Guardrails().check(buy(qty=20), account(cash=1_000))


def test_config_cannot_loosen_hard_limits():
    with pytest.raises(LimitsError):
        Limits.from_config({"max_daily_loss_pct": 20})
    with pytest.raises(LimitsError):
        Limits.from_config({"max_daily_loss_pct": 0})
    with pytest.raises(LimitsError):
        Limits.from_config({"typo_limit": 5})
    assert Limits.from_config({"max_position_pct": 10}).max_position_pct == 10


def test_unmanaged_positions():
    problems = find_unmanaged_positions(["VRT", "AAPL"], symbols_with_stop=["AAPL"], managed_symbols=["AAPL"])
    assert set(problems) == {"VRT"}
    assert len(problems["VRT"]) == 2
