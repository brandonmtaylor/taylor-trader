from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from taylortrader import chart, grading
from taylortrader.stats import Fill

T0 = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)  # Monday 9:30 New York


def hourly(closes, start=T0):
    idx = pd.date_range(start, periods=len(closes), freq="h", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def decision(ts, action="hold", question="entry", conf=0.7, symbol="NVDA", **kw):
    return {"id": kw.get("id", 1), "ts": ts.isoformat(), "strategy": "ai", "symbol": symbol, "question": question,
            "action": action, "confidence": conf, "acted": 0, "summary": "s", "error": kw.get("error")}


def test_outcome_starts_from_the_last_finished_bar():
    df = hourly([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    # Asked 2h10m after the first bar opened: bars 0 and 1 had finished, so the price is bar 1's close.
    o = grading.outcome(df, T0 + timedelta(hours=2, minutes=10))
    assert o["price"] == 101
    assert o["ret"] == pytest.approx((108 / 101 - 1) * 100)   # close 7 bars later (bar 8)
    assert o["up"] == pytest.approx((109 / 101 - 1) * 100)
    assert grading.outcome(df, T0 + timedelta(hours=5)) is None  # the day hasn't played out yet


@pytest.mark.parametrize(("question", "action", "ret", "want"), [
    ("entry", "buy", 2.0, "right"), ("entry", "buy", -2.0, "wrong"),
    ("entry", "hold", -2.0, "right"), ("entry", "hold", 2.0, "wrong"),
    ("exit", "sell", -2.0, "right"), ("exit", "hold", -2.0, "wrong"),
    ("entry", "buy", 0.5, "flat"),
])
def test_verdicts(question, action, ret, want):
    assert grading.verdict(question, action, ret) == want


def test_report_compares_ai_buys_with_the_rule_alone():
    up = hourly([100] * 2 + [100 + i for i in range(1, 15)])          # rises steadily
    down = hourly([100] * 2 + [100 - i for i in range(1, 15)], )        # falls steadily
    ds = [decision(T0 + timedelta(hours=1), "buy", symbol="UP", id=1),
          decision(T0 + timedelta(hours=1), "hold", symbol="DOWN", id=2, conf=0.55),
          decision(T0 + timedelta(hours=1), symbol="DOWN", id=3, error="timeout"),          # not graded
          decision(T0 + timedelta(hours=14), "hold", symbol="UP", id=4)]                    # too recent
    r = grading.report(grading.grade(ds, {"UP": up, "DOWN": down}))
    assert r["graded"] == 2 and r["pending"] == 1
    assert r["overall"]["right"] == 2 and r["overall"]["accuracy"] == 100
    assert r["ai_buys"]["avg_ret"] > 0 > r["by_call"]["entry:hold"]["avg_ret"]
    assert r["rule_only"]["n"] == 2  # every entry question the rule let through, bought blindly
    assert [b["n"] for b in r["confidence"]] == [1, 0, 1]


def test_chart_marks_trades_and_measures_timing():
    df = hourly([100, 102, 110, 120, 115, 105, 108, 125, 130, 101])
    now = T0 + timedelta(hours=10)
    fills = [Fill(T0 + timedelta(minutes=5), "VRT", "buy", 2, 100.0, "b1"),
             Fill(T0 + timedelta(hours=5, minutes=5), "VRT", "sell", 2, 105.0, "s1"),
             Fill(T0 + timedelta(hours=9, minutes=5), "VRT", "buy", 1, 101.0, "b2"),
             Fill(T0, "AAPL", "buy", 1, 50.0, "x")]
    ai = [decision(T0 + timedelta(hours=2), "buy", symbol="VRT"), decision(T0, symbol="NVDA")]
    out = chart.build("VRT", "1W", df, fills, {"b1": "macd_rsi"}, ai, stop=90.0, held_qty=1, now=now)
    assert [(m["i"], m["side"]) for m in out["marks"]] == [(0, "buy"), (5, "sell"), (9, "buy")]
    assert out["marks"][0]["strategy"] == "macd_rsi"
    t = out["trades"][0]
    assert t["peak"] == 121  # highest high while held (bar 3)
    assert t["captured"] == pytest.approx(5 / 21 * 100)  # kept $5 of a possible $21
    assert t["after_high"] == 131 and not t["after_complete"]
    assert out["position"]["entry"] == 101 and out["position"]["stop"] == 90
    assert [a["i"] for a in out["ai"]] == [2]  # only this stock's calls
