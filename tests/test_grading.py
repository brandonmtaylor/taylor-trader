from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from taylortrader import chart, grading
from taylortrader.data import regular_hours
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
    assert o["ret"] == pytest.approx((107 / 101 - 1) * 100)   # close 6 bars later (bar 7)
    assert o["up"] == pytest.approx((108 / 101 - 1) * 100)
    assert grading.outcome(df, T0 + timedelta(hours=5)) is None  # the day hasn't played out yet


def test_a_day_is_six_hourly_bars_because_the_9_00_bar_is_dropped():
    day = pd.date_range("2026-11-02 14:00", "2026-11-02 21:00", freq="h", tz="UTC")  # 9:00-16:00 New York
    kept = regular_hours(pd.DataFrame({"close": 1.0}, index=day))
    assert len(kept) == grading.HORIZON_BARS == 6
    assert grading.report([])["horizon"] == "the next trading day (6 hourly bars)"


@pytest.mark.parametrize(("bar", "n"), [(timedelta(minutes=1), 390), (timedelta(minutes=5), 78),
                                        (timedelta(minutes=15), 26), (timedelta(hours=1), 6), (timedelta(days=1), 1)])
def test_a_trading_day_in_bars(bar, n):
    assert grading.horizon_bars(bar) == n


def test_grading_uses_the_real_bar_size():
    bar = timedelta(minutes=15)
    idx = pd.date_range(T0, periods=40, freq="15min", tz="UTC")
    c = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})
    # Asked as bar 2 ended: bars 0-2 had finished. An hourly assumption would pick an older bar.
    g = grading.grade([decision(idx[3].to_pydatetime())], {"NVDA": df}, bar=bar)[0]
    assert g["price"] == 102
    assert g["ret"] == pytest.approx((128 / 102 - 1) * 100)  # a full day: 26 bars later
    assert grading.report([g], bar=bar)["horizon"] == "the next trading day (26 bars)"


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


def test_open_position_shows_the_newest_buy_when_fills_come_newest_first():
    df = hourly([100, 102, 110, 120, 115, 105, 108, 125, 130, 101])
    fills = [Fill(T0 + timedelta(hours=6), "VRT", "buy", 1, 108.0, "b2"),
             Fill(T0 + timedelta(hours=4), "VRT", "sell", 1, 115.0, "s1"),
             Fill(T0 + timedelta(hours=1), "VRT", "buy", 1, 102.0, "b1")]
    out = chart.build("VRT", "1W", df, fills, {}, [], stop=None, held_qty=1, now=T0 + timedelta(hours=10))
    assert out["position"]["entry"] == 108.0
    assert out["position"]["opened"] == (T0 + timedelta(hours=6)).isoformat()


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


def test_breakdown_hides_thin_slices():
    rows = []
    for i in range(12):
        rows.append({"id": i, "ts": T0.isoformat(), "question": "entry", "action": "hold", "confidence": 0.7,
                     "ret": -2.0, "verdict": "right", "features_saved": True,
                     "features": {"close": 100, "ma_200": 110, "ma_20": 101, "rsi": 25 if i < 11 else 60,
                                  "macd_diff": -0.1, "chaikin": -0.05}})
    r = grading.report(rows)
    rsi = next(g for g in r["by_indicator"] if g["group"] == "RSI")["slices"]
    assert rsi[0]["n"] == 11 and rsi[0]["enough"] and rsi[0]["accuracy"] == 100
    assert rsi[2]["n"] == 1 and not rsi[2]["enough"]
    trend = next(g for g in r["by_indicator"] if g["group"] == "Long trend")["slices"]
    assert trend[1]["n"] == 12 and trend[0]["n"] == 0  # all below the 200-bar average


def test_older_calls_get_recalculated_values():
    df = hourly([100 + (i % 5) for i in range(260)], start=T0 - timedelta(days=60))
    d = decision(df.index[250].to_pydatetime() + timedelta(minutes=30))
    g = grading.grade([d], {"NVDA": df})[0]
    assert not g["features_saved"] and 0 <= g["features"]["rsi"] <= 100 and "ma_200" in g["features"]


def test_ai_call_saves_the_indicators_it_was_shown(tmp_path):
    from taylortrader.journal import Journal
    from taylortrader.strategies.ai import features

    j = Journal(tmp_path / "j.db")
    f = features({"close": 101.234567, "rsi": 28.1, "ma_200": float("nan"), "volume": 5})
    assert f == {"close": 101.2346, "rsi": 28.1}  # missing/NaN values left out, nothing extra
    j.record_ai(strategy="ai", symbol="NVDA", question="entry", action="hold", confidence=0.6, features=f)
    assert j.recent_ai(1)[0]["features"] == f


def test_old_journal_gains_the_features_column(tmp_path):
    import sqlite3

    from taylortrader.journal import Journal

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL,"
               " symbol TEXT NOT NULL, question TEXT NOT NULL, action TEXT, confidence REAL, summary TEXT,"
               " pros TEXT, cons TEXT, acted INTEGER NOT NULL DEFAULT 0, model TEXT, input_tokens INTEGER,"
               " output_tokens INTEGER, latency_ms INTEGER, error TEXT)")
    db.commit()
    db.close()
    j = Journal(path)
    j.record_ai(strategy="ai", symbol="X", question="entry", action="hold", features={"rsi": 40.0})
    assert j.recent_ai(1)[0]["features"] == {"rsi": 40.0}


def test_chart_points_carry_indicator_values():
    from taylortrader.indicators import add_all

    df = add_all(hourly([100 + (i % 7) for i in range(300)], start=T0 - timedelta(days=40)))
    out = chart.build("VRT", "1W", df, [], {}, [], None, 0, now=df.index[-1].to_pydatetime())
    last = out["points"][-1]
    assert {"ma_20", "ma_200", "rsi", "macd_diff"} <= last.keys()
