from datetime import UTC, datetime, timedelta

import pytest

from taylortrader.stats import Fill, round_trips, summarize

T = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def f(h, sym, side, qty, px, oid="x"):
    return Fill(T + timedelta(hours=h), sym, side, qty, px, oid)


def test_fifo_pairing_partial_sells_and_streaks():
    fills = [
        f(0, "A", "buy", 10, 10.0, "b1"), f(1, "A", "buy", 10, 12.0, "b2"),
        f(2, "A", "sell", 15, 11.0),       # 10 from b1 (+$10), 5 from b2 (-$5)
        f(3, "A", "sell", 5, 13.0),        # rest of b2 (+$5)
        f(4, "B", "buy", 4, 50.0, "b3"), f(5, "B", "sell", 4, 40.0),  # -$40
        f(6, "C", "buy", 1, 10.0, "b4"),   # still open: not a trade
    ]
    trades = round_trips(fills, {"b1": "s", "b2": "s", "b3": "s"})
    assert [(t.symbol, t.qty, round(t.pnl, 2)) for t in trades] == [("A", 10, 10), ("A", 5, -5), ("A", 5, 5), ("B", 4, -40)]
    s = summarize(trades, T.date())
    assert s["wins"] == 2 and s["losses"] == 2 and s["win_rate"] == 50
    assert s["profit_factor"] == pytest.approx(15 / 45)
    assert s["last"] == ["W", "L", "W", "L"] and s["streak"] == {"kind": "L", "n": 1}
    assert s["today"]["trades"] == 4  # T is 10am New York on the same day


def test_empty():
    s = summarize([], T.date())
    assert s["trades"] == 0 and s["win_rate"] is None and s["streak"] is None
