"""What an invest plan would have done in the past: put in vs. worth now, and the worst drop.

Replays a plan day by day on daily closes, with the same schedule (invest.due_period) and the same
"new money goes to whatever is furthest under its target" split as the real plan. Prices are
dividend-adjusted, so dividends count as reinvested. No taxes, no fees (Alpaca charges none on
buys), buys at the day's close.

The worst drop is measured like a fund's: on the value of one "share" of the plan, so new money
going in doesn't hide a fall. It's the largest peak-to-trough loss someone in the plan sat through.

Also: the same total put in all at once on the first day, for comparison.

This is the past, not a forecast.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from taylortrader.config import InvestConfig
from taylortrader.invest import NY, due_period, split


def _closes(prices: dict[str, pd.DataFrame], symbols: list[str], start: date) -> pd.DataFrame:
    """One close per trading day (New York date) per symbol, from the first day they all trade."""
    cols = {}
    for s in symbols:
        df = prices[s]
        c = df["close"].copy()
        # Daily bars are stamped anywhere from midnight UTC (the evening before, in New York) to the
        # close (21:00 UTC). Shifting 6 hours lands every one of those on its own New York trading day.
        idx = c.index if c.index.tz is not None else c.index.tz_localize("UTC")
        c.index = (idx + pd.Timedelta(hours=6)).tz_convert(NY).date
        cols[s] = c[~c.index.duplicated(keep="last")]
    frame = pd.DataFrame(cols).dropna()
    return frame[frame.index >= start]


def simulate(prices: dict[str, pd.DataFrame], plan: InvestConfig, start: date) -> dict | None:
    weights = plan.weights
    closes = _closes(prices, list(weights), start)
    if len(closes) < 20:
        return None
    first = closes.index[0]
    sched = InvestConfig(**{**plan.__dict__, "start": first.isoformat(), "until": ""})

    units = {s: 0.0 for s in weights}
    put_in = 0.0
    done: set[str] = set()
    fund_units = 0.0     # "shares" of the plan, for measuring drops without contributions in the way
    peak_nav, worst = 0.0, 0.0
    points = []
    for d, row in closes.iterrows():
        value = sum(units[s] * row[s] for s in weights)
        nav = value / fund_units if fund_units else 1.0
        if fund_units:
            peak_nav = max(peak_nav, nav)
            worst = min(worst, nav / peak_nav - 1)
        p = due_period(sched, d)
        if p and p not in done and (plan.every != "once" or not done):
            done.add(p)
            alloc = split(plan.amount, {s: units[s] * row[s] for s in weights}, weights)
            for s, dollars in alloc.items():
                units[s] += dollars / row[s]
            added = sum(alloc.values())
            put_in += added
            fund_units += added / nav
            value += added
        points.append((d, put_in, value))

    last = closes.iloc[-1]
    value = sum(units[s] * last[s] for s in weights)
    # The same money, all on the first day, in the target weights.
    first_row = closes.iloc[0]
    lump_units = {s: put_in * w / sum(weights.values()) / first_row[s] for s, w in weights.items()}
    lump_path = sum(lump_units[s] * closes[s] for s in weights)
    lump_worst = float((lump_path / lump_path.cummax() - 1).min())

    monthly = pd.DataFrame(points, columns=["d", "put_in", "value"]).groupby(
        pd.to_datetime([p[0] for p in points]).to_period("M")).last()
    ts = [int(pd.Timestamp(d).timestamp()) for d in monthly["d"]]
    return {
        "from": first.isoformat(), "to": closes.index[-1].isoformat(), "contributions": len(done),
        "put_in": put_in, "value": value, "gain_pct": (value / put_in - 1) * 100 if put_in else None,
        "worst_drop_pct": worst * 100,
        "lump": {"value": float(lump_path.iloc[-1]), "gain_pct": (float(lump_path.iloc[-1]) / put_in - 1) * 100
                 if put_in else None, "worst_drop_pct": lump_worst * 100},
        "series": {"value": [{"t": t, "v": round(float(v), 2)} for t, v in zip(ts, monthly["value"], strict=True)],
                   "put_in": [{"t": t, "v": round(float(v), 2)} for t, v in zip(ts, monthly["put_in"], strict=True)]},
    }
