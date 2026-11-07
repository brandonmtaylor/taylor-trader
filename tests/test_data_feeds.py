"""Feed fallback: Yahoo is unofficial, so the service must keep trading on IEX when it breaks."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from taylortrader.data import FallbackData

# Tuesday 2026-11-03, 11:00 New York time
NOW = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)


def frame(last: datetime, n: int = 5) -> pd.DataFrame:
    idx = pd.DatetimeIndex([last - timedelta(minutes=5 * i) for i in reversed(range(n))], tz=UTC)
    return pd.DataFrame({c: 1.0 for c in ["open", "high", "low", "close", "volume"]}, index=idx)


class Feed:
    def __init__(self, feed, result=None, error=None, timeframes=None):
        self.feed, self.result, self.error, self.calls = feed, result, error, 0
        self.timeframes = timeframes

    def supports(self, tf):
        return self.timeframes is None or tf in self.timeframes

    def bars(self, *a, **k):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def make(primary, fallback=None, now=NOW):
    switches = []
    fb = fallback or Feed("iex", frame(now - timedelta(minutes=5)))
    data = FallbackData(primary, fb, on_switch=lambda m, lvl: switches.append((lvl, m)), now=lambda: now)
    return data, fb, switches


def test_fresh_primary_is_used():
    data, fb, switches = make(Feed("yahoo", frame(NOW - timedelta(minutes=5))))
    data.bars("VRT", "5Min", NOW - timedelta(days=1))
    assert fb.calls == 0 and switches == [] and data.using == "yahoo"


@pytest.mark.parametrize("primary", [
    Feed("yahoo", error=RuntimeError("429 Too Many Requests")),
    Feed("yahoo", pd.DataFrame(columns=["open", "high", "low", "close", "volume"])),
    Feed("yahoo", frame(NOW - timedelta(minutes=30))),  # lagging
])
def test_bad_primary_falls_back_and_says_so_once(primary):
    data, fb, switches = make(primary)
    data.bars("VRT", "5Min", NOW - timedelta(days=1))
    data.bars("VRT", "5Min", NOW - timedelta(days=1))
    assert fb.calls == 2
    assert len(switches) == 1 and switches[0][0] == "warning" and data.using == "iex"


def test_recovery_switches_back():
    primary = Feed("yahoo", error=RuntimeError("down"))
    data, _, switches = make(primary)
    data.bars("VRT", "5Min", NOW)
    primary.error, primary.result = None, frame(NOW - timedelta(minutes=5))
    data.bars("VRT", "5Min", NOW)
    assert [lvl for lvl, _ in switches] == ["warning", "info"] and data.using == "yahoo"


def test_old_bars_outside_market_hours_are_not_stale():
    saturday = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
    data, fb, _ = make(Feed("yahoo", frame(saturday - timedelta(days=1))), now=saturday)
    data.bars("VRT", "5Min", saturday - timedelta(days=3))
    assert fb.calls == 0


def test_unsupported_timeframe_goes_straight_to_fallback():
    primary = Feed("yahoo", frame(NOW), timeframes={"5Min"})
    data, fb, switches = make(primary, Feed("iex", frame(NOW, 2)))
    data.bars("VRT", "1Hour", NOW - timedelta(days=5))
    assert primary.calls == 0 and fb.calls == 1 and switches == []


def test_a_split_rebuilds_the_cache_instead_of_showing_a_crash(tmp_path):
    from taylortrader.data import MarketData

    def bars(start, closes):
        idx = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
        return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=idx)

    md = MarketData.__new__(MarketData)
    md.cache_dir, md.feed = tmp_path, "iex"
    md._fetch = lambda *a: bars("2026-01-01", [100.0, 100.0, 100.0])
    start = datetime(2026, 1, 1, tzinfo=UTC)
    md.bars("X", "1Day", start, datetime(2026, 1, 3, tzinfo=UTC))
    calls = []

    def after_split(symbol, tf, s, e):              # a 2:1 split: every bar now reads half
        calls.append(s)
        return bars("2026-01-03", [50.0, 50.0]) if len(calls) == 1 else bars("2026-01-01", [50.0] * 4)

    md._fetch = after_split
    df = md.bars("X", "1Day", start, datetime(2026, 1, 4, tzinfo=UTC))
    assert len(calls) == 2 and set(df["close"]) == {50.0}


def test_hourly_bars_start_at_the_open(tmp_path):
    from taylortrader.data import MarketData

    # Tuesday 9:30-16:00 New York in half hours (14:30-21:00 UTC), close = minute of the day
    idx = pd.date_range("2026-11-03 14:30", periods=13, freq="30min", tz="UTC")
    half = pd.DataFrame({"open": range(13), "high": range(13), "low": range(13), "close": range(13),
                         "volume": 1.0}, index=idx).astype(float)
    asked = []
    md = MarketData.__new__(MarketData)
    md.cache_dir, md.feed = tmp_path, "iex"
    md._fetch = lambda sym, tf, s, e: asked.append(tf) or half
    out = md.bars("VRT", "1Hour", datetime(2026, 11, 3, 14, 0, tzinfo=UTC), datetime(2026, 11, 3, 22, 0, tzinfo=UTC))
    times = [t.strftime("%H:%M") for t in out.index.tz_convert("America/New_York")]
    assert asked == ["30Min"]
    assert times == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    first = out.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (0, 1, 0, 1, 2)
    assert out.iloc[-1].volume == 1  # 15:30-16:00 is half an hour

