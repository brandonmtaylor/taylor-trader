import numpy as np
import pandas as pd
import pytest

from taylortrader import whatif
from taylortrader.config import InvestConfig

START = pd.Timestamp("2024-01-02").date()


def daily(prices, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(prices), tz="America/New_York") + pd.Timedelta(hours=16)
    return pd.DataFrame({"close": np.asarray(prices, dtype=float)}, index=idx.tz_convert("UTC"))


def plan(holdings, amount=100.0, every="month"):
    return InvestConfig(enabled=True, amount=amount, every=every, day=1, holdings=holdings)


def test_flat_prices_are_worth_what_went_in():
    n = 260  # about a year of trading days
    r = whatif.simulate({"A": daily([50] * n), "B": daily([20] * n)},
                        plan([{"symbol": "A", "weight": 60.0}, {"symbol": "B", "weight": 40.0}]), START)
    assert r["contributions"] == 12 and r["put_in"] == pytest.approx(1200)
    assert r["value"] == pytest.approx(1200) and r["worst_drop_pct"] == pytest.approx(0)
    assert r["lump"]["value"] == pytest.approx(1200)


def test_a_crash_shows_even_while_new_money_hides_it():
    # Halves over two months, then recovers. Monthly buys keep the dollar balance from showing the fall.
    prices = list(np.linspace(100, 50, 45)) + list(np.linspace(50, 100, 215))
    r = whatif.simulate({"A": daily(prices)}, plan([{"symbol": "A", "weight": 100.0}]), START)
    assert r["worst_drop_pct"] == pytest.approx(-50, abs=1)
    assert r["value"] > r["put_in"]                         # bought cheap on the way down
    assert r["lump"]["worst_drop_pct"] == pytest.approx(-50, abs=1)
    assert r["lump"]["value"] == pytest.approx(r["put_in"], rel=0.01)   # all in at 100, ends at 100


def test_a_drop_right_after_the_first_buy_counts():
    prices = [100.0, 80.0] + list(np.linspace(80, 130, 258))  # falls the day after the first buy, never again
    r = whatif.simulate({"A": daily(prices)}, plan([{"symbol": "A", "weight": 100.0}]), START)
    assert r["worst_drop_pct"] == pytest.approx(-20, abs=0.5)


def test_all_at_once_is_one_buy_and_series_are_monthly():
    r = whatif.simulate({"A": daily(np.linspace(100, 120, 300))}, plan([{"symbol": "A", "weight": 100.0}],
                                                                      amount=5000, every="once"), START)
    assert r["contributions"] == 1 and r["put_in"] == pytest.approx(5000)
    assert r["value"] == pytest.approx(6000, rel=0.001)
    assert 13 <= len(r["series"]["value"]) <= 15


def test_starts_when_every_fund_has_prices():
    late = daily([10] * 100, start="2024-06-03")
    r = whatif.simulate({"A": daily([50] * 260), "B": late},
                        plan([{"symbol": "A", "weight": 50.0}, {"symbol": "B", "weight": 50.0}]), START)
    assert r["from"] == "2024-06-03"


@pytest.mark.parametrize("stamp", ["00:00", "05:00", "14:30", "21:00"])  # UTC: midnight, NY midnight, open, close
def test_daily_bars_land_on_their_own_trading_day_however_they_are_stamped(stamp):
    idx = pd.bdate_range("2024-01-02", periods=30, tz="UTC") + pd.Timedelta(stamp + ":00")
    df = pd.DataFrame({"close": 10.0}, index=idx)
    r = whatif.simulate({"A": df}, plan([{"symbol": "A", "weight": 100.0}]), START)
    assert r["from"] == "2024-01-02" and r["to"] == str(idx[-1].date())
