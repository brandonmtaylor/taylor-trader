import numpy as np
import pandas as pd
import pytest

from guardrails import OrderIntent, Side
from taylortrader import backtest, indicators
from taylortrader.broker import AlpacaBroker, BrokerError
from taylortrader.strategies import Bars, Signal, Strategy, get_strategy, list_strategies


def bars_from_closes(closes, start="2026-01-02 14:30", freq="5min"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes) * 1.001, "low": np.minimum(opens, closes) * 0.999,
         "close": closes, "volume": 1_000.0},
        index=idx,
    )


def test_registry_lists_all_ported_strategies():
    names = {s["name"] for s in list_strategies()}
    assert {"macd_rsi", "macd", "ma_20", "rsi_14", "ma_20_rsi", "ma_200_rsi",
            "ma_20_over_200_rsi", "chaikin", "pullback"} <= names


def test_params_are_validated():
    assert get_strategy("macd_rsi", {"rsi_buy": 30}).p.rsi_buy == 30
    with pytest.raises(ValueError, match="unknown parameter"):
        get_strategy("macd_rsi", {"rsi_buyy": 30})
    with pytest.raises(ValueError, match="unknown strategy"):
        get_strategy("nope")


def test_indicators_are_causal():
    df = bars_from_closes(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 400)))
    full = indicators.add_all(df)
    part = indicators.add_all(df.iloc[:300])
    pd.testing.assert_frame_equal(full.iloc[:300], part)


class AlwaysIn(Strategy):
    name = "always_in"
    description = "test"

    class Params:  # no params
        pass

    def __init__(self):
        self.p = None

    @property
    def warmup(self):
        return 1

    def entry(self, b: Bars, i: int) -> Signal:
        return Signal(True, "test entry")

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        return Signal(False)


def test_backtest_stop_fills_at_stop_or_gap_open():
    closes = [100, 100, 100, 100, 95, 70, 70, 70]  # gap down through a 12% stop
    df = bars_from_closes(closes)
    r = backtest.run(df, AlwaysIn(), stop_loss_pct=12, slippage_bps=0)
    first = r.trades[0]
    assert first.exit_reason.startswith("stop-loss")
    # bar 5 opens at 95 (above the 88 stop) and trades down to ~69.9, so it fills at the stop
    assert first.exit_price == pytest.approx(88.0)


def test_backtest_reports_buy_and_hold():
    df = bars_from_closes(np.linspace(100, 200, 300))
    strat = get_strategy("rsi_14")
    r = backtest.run(df, strat)
    # buy-and-hold is measured from the first bar after warmup, same window as the strategy
    expected = (df.close.iloc[-1] / df.open.iloc[strat.warmup + 1] - 1) * 100
    assert r.buy_hold_return_pct == pytest.approx(expected)
    assert "vs_buy_hold_pts" in r.summary()


class FakeClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return req


def _broker():
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.paper = True
    b._client = FakeClient()
    return b


def test_broker_entry_always_has_broker_side_stop():
    b = _broker()
    req = b.submit_entry(OrderIntent("VRT", Side.BUY, 10.7, 224.0, 197.12))
    assert req.qty == 10 and req.stop_loss.stop_price == 197.12
    assert req.order_class.value == "oto" and req.time_in_force.value == "gtc"
    req = b.submit_entry(OrderIntent("VRT", Side.BUY, 10, 224.0, 197.12, 250.0))
    assert req.order_class.value == "bracket" and req.take_profit.limit_price == 250.0


def test_broker_refuses_entry_without_stop():
    with pytest.raises(BrokerError):
        _broker().submit_entry(OrderIntent("VRT", Side.BUY, 10, 224.0, None))
