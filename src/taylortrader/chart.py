"""One stock's price with the bot's buys and sells on it, and how well each trade was timed.

For each closed trade: the highest price while it was held (how much of the run-up the sell
kept) and the highest price in the week after the sell (what selling left on the table).
For an open position: the high since buying and where the stop-loss sits.

Markers carry the index of the bar they fall in, so the chart can space bars evenly and skip
nights and weekends without each marker having to be placed by time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from taylortrader.stats import Fill, round_trips, utc_ts

# period -> (days back, bar size)
PERIODS = {"1W": (7, "15Min"), "1M": (31, "1Hour"), "3M": (92, "1Day"), "1A": (366, "1Day")}
# Extra calendar days fetched before the window so the 200-bar average exists from its first point.
WARMUP_DAYS = {"15Min": 18, "1Hour": 62, "1Day": 330}  # ~200 bars plus holidays
LINES = ("ma_20", "ma_200", "rsi", "macd", "macd_signal", "macd_diff")
AFTER_SELL = timedelta(days=7)  # "what happened next" window: about five trading days
MAX_POINTS = 600


def _index(df: pd.DataFrame, when) -> int | None:
    """The bar a moment falls in: the last bar starting at or before it."""
    pos = int((df.index <= utc_ts(when)).sum()) - 1  # (searchsorted trips over mixed time units)
    return pos if 0 <= pos < len(df) else None


def _high(df: pd.DataFrame, start, end) -> float | None:
    part = df[(df.index >= _bar_start(df, start)) & (df.index <= utc_ts(end))]
    return float(part["high"].max()) if not part.empty else None


def _bar_start(df: pd.DataFrame, when) -> pd.Timestamp:
    i = _index(df, when)
    return df.index[i] if i is not None else utc_ts(when)


def build(symbol: str, period: str, df: pd.DataFrame, fills: list[Fill], strategy_of: dict[str, str],
          decisions: list[dict], stop: float | None, held_qty: float, now: datetime,
          target: float | None = None) -> dict:
    days, timeframe = PERIODS[period]
    start = utc_ts(now) - timedelta(days=days)
    df = df[df.index >= start] if not df.empty else df
    mine = [f for f in fills if f.symbol == symbol]

    step = max(1, -(-len(df) // MAX_POINTS))
    keep = list(range(0, len(df), step))
    if keep and keep[-1] != len(df) - 1:
        keep.append(len(df) - 1)
    lines = [k for k in LINES if k in df.columns]

    def pt(i: int) -> dict:
        row = df.iloc[i]
        out = {"t": int(df.index[i].timestamp()), "c": round(float(row["close"]), 4)}
        for k in lines:
            v = row[k]
            if pd.notna(v):
                out[k] = round(float(v), 4)
        return out

    points = [pt(i) for i in keep]
    to_point = {i: n for n, i in enumerate(keep)}

    def point(when) -> int | None:
        i = _index(df, when)
        if i is None or utc_ts(when) > utc_ts(now):
            return None
        # nearest kept point at or before the bar (only differs when the series was thinned)
        while i not in to_point and i > 0:
            i -= 1
        return to_point.get(i)

    marks = []
    for f in sorted(mine, key=lambda f: f.ts):
        p = point(f.ts)
        if p is not None and utc_ts(f.ts) >= start:
            marks.append({"i": p, "t": utc_ts(f.ts).isoformat(), "side": f.side, "qty": f.qty, "price": f.price,
                          "strategy": strategy_of.get(f.order_id) if f.side == "buy" else None})

    trades = []
    for t in round_trips(mine, strategy_of):
        if utc_ts(t.closed) < start:
            continue
        covered = not df.empty and df.index[0] <= utc_ts(t.opened)
        peak = _high(df, t.opened, t.closed) if covered else None
        peak = max(peak, t.exit, t.entry) if peak is not None else None
        after_end = min(utc_ts(t.closed) + AFTER_SELL, utc_ts(now))
        later = df[(df.index > utc_ts(t.closed)) & (df.index <= after_end)] if not df.empty else df
        after_high = float(later["high"].max()) if not later.empty else None
        trades.append({
            **t.as_dict(),
            "peak": peak,
            "peak_pct": (peak / t.entry - 1) * 100 if peak else None,
            # Share of the best possible gain the sell kept: 0 to 100, and 0 for a sale at a loss (a loss
            # divided by a small gain made numbers like -1100%). None if the price never rose above the buy.
            "captured": (max(0.0, (t.exit - t.entry) / (peak - t.entry) * 100)
                         if peak and peak > t.entry * 1.0005 else None),
            "after_high": after_high,
            "after_pct": (after_high / t.exit - 1) * 100 if after_high else None,
            "after_complete": utc_ts(t.closed) + AFTER_SELL <= utc_ts(now),
        })
    trades.reverse()

    position = None
    if held_qty > 0:
        buys = [f for f in mine if f.side == "buy"]
        last_buy = max(buys, key=lambda f: f.ts, default=None)  # fills can arrive newest first
        last = float(df["close"].iloc[-1]) if not df.empty else None
        entry = last_buy.price if last_buy else None
        since = df[df.index >= _bar_start(df, last_buy.ts)] if (last_buy and not df.empty) else df.iloc[0:0]
        peak = float(since["high"].max()) if not since.empty else None
        position = {"qty": held_qty, "entry": entry, "opened": utc_ts(last_buy.ts).isoformat() if last_buy else None,
                    "last": last, "stop": stop, "target": target, "peak": peak,
                    "peak_pct": (peak / entry - 1) * 100 if (peak and entry) else None,
                    "pct": (last / entry - 1) * 100 if (last and entry) else None}

    ai = []
    for d in decisions:
        if d["symbol"] != symbol or d.get("error") or not d.get("action") or utc_ts(d["ts"]) < start:
            continue
        p = point(d["ts"])
        if p is not None:
            ai.append({"i": p, "t": utc_ts(d["ts"]).isoformat(), "action": d["action"], "question": d["question"],
                       "confidence": d["confidence"], "acted": bool(d["acted"]), "summary": d.get("summary")})

    return {"symbol": symbol, "period": period, "timeframe": timeframe, "points": points, "marks": marks,
            "trades": trades, "position": position, "ai": ai}
