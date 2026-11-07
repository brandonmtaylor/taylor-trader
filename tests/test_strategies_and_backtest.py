from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from alpaca.trading.enums import OrderSide, OrderType

from taylortrader import backtest, indicators
from taylortrader.broker import AlpacaBroker, BrokerError
from taylortrader.guardrails import OrderIntent, Side
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


def test_idle_cash_earns_interest_when_asked():
    import pandas as pd

    from taylortrader import backtest
    from taylortrader.strategies import get_strategy

    idx = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
    c = pd.Series(100.0, index=idx)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6})
    never = get_strategy("rule", {"buy_when": "rsi > 1000", "sell_when": ""})  # never buys: all cash
    flat = backtest.run(df, never)
    paid = backtest.run(df, never, cash_yield_pct=4.0)
    assert flat.total_return_pct == 0
    days = (idx[-1] - idx[never.warmup]).days
    assert abs(paid.total_return_pct - ((1.04 ** (days / 365.25)) - 1) * 100) < 0.01


class FlowClient:
    def __init__(self, rows):
        self.rows, self.gets = rows, 0
        self.account = SimpleNamespace(equity="1100", cash="1100", last_equity="1000")

    def get_calendar(self, req):
        return [SimpleNamespace(date=date(2026, 11, 5))]

    def get(self, path, data):
        self.gets += 1
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    def get_account(self):
        return self.account


def _flow_broker(rows):
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.paper, b._client, b._cash_moved, b._last_session = True, FlowClient(rows), (0.0, 0.0), None
    b.positions = dict
    b.trades_today = lambda: 0
    return b


def test_a_deposit_doesnt_hide_a_loss_and_a_withdrawal_isnt_one():
    deposit = [{"date": "2026-11-06", "net_amount": "150.00"}]
    snap = _flow_broker(deposit).snapshot()
    assert snap.day_start_equity == 1150 and snap.day_pnl_pct < 0  # $1,000 + $150 in, worth $1,100: a loss
    withdrawal = [{"date": "2026-11-06", "net_amount": "-200"}]
    assert _flow_broker(withdrawal).snapshot().day_start_equity == 800


def test_money_moved_before_the_last_close_is_already_in_the_start():
    assert _flow_broker([{"date": "2026-11-05", "net_amount": "500"}]).snapshot().day_start_equity == 1000


def test_cash_moved_is_checked_every_few_minutes_and_survives_a_failure():
    b = _flow_broker([{"date": "2026-11-06", "net_amount": "50"}])
    assert b.cash_moved_today() == 50 and b.cash_moved_today() == 50
    assert b._client.gets == 1
    b._cash_moved = (1.0, 50.0)  # long ago
    b._client.rows = ConnectionError("down")
    assert b.cash_moved_today() == 50
    assert _flow_broker(ConnectionError("down")).snapshot().day_start_equity == 1000


# --- trades per day ---------------------------------------------------------------------------------

class OrdersClient:
    def __init__(self, orders):
        self.orders, self.requests = orders, []

    def get_orders(self, req):
        self.requests.append(req)
        return self.orders


def _order(cid, status="filled", filled=1.0):
    return SimpleNamespace(client_order_id=cid, status=SimpleNamespace(value=status), filled_qty=filled)


def test_trades_today_counts_only_strategy_buys():
    b = AlpacaBroker.__new__(AlpacaBroker)
    b._client = OrdersClient([
        _order("tt-macd_rsi-VRT-1"), _order("tt-ai-NVDA-2", "new", 0),  # filled, and one still working
        _order("tt-invest-2026-11-VTI-1"), _order(None), _order("manual-order"),  # plan, yours, yours
        _order("tt-breakout-AAPL-3", "canceled", 0), _order("tt-breakout-AAPL-4", "canceled", 2.0),
    ])
    assert b.trades_today() == 3
    req = b._client.requests[0]
    assert req.side == OrderSide.BUY and req.after.tzinfo is not None and req.after.hour == 0
    b._client.orders = [_order("tt-x-Y-1")] * 500
    assert b.trades_today() == 500  # a full page can't be counted: assume the limit is hit


def test_a_refused_sell_puts_the_stop_back():
    class Client:
        def __init__(self):
            self.cancelled, self.submitted = [], []

        def get_orders(self, req):
            stop = SimpleNamespace(id="s1", symbol="VRT", status="new", side=OrderSide.SELL, order_type=OrderType.STOP,
                                   stop_price="158.62", qty="3", legs=None)
            return [] if self.cancelled else [stop]

        def cancel_order_by_id(self, oid):
            self.cancelled.append(oid)

        def close_position(self, symbol):
            raise RuntimeError("trade denied due to pattern day trading protection")

        def submit_order(self, req):
            self.submitted.append(req)

    b = AlpacaBroker.__new__(AlpacaBroker)
    b.paper, b._client = True, Client()
    with pytest.raises(RuntimeError, match="pattern day"):
        b.close_position("VRT")
    assert b._client.cancelled == ["s1"]
    req = b._client.submitted[0]
    assert req.symbol == "VRT" and float(req.qty) == 3 and float(req.stop_price) == 158.62
