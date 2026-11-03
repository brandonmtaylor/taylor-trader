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
