"""Grade the AI's decisions against what the price did next.

Each decision is scored on the next trading day (6 hourly bars) after it was made, starting from
the close of the last bar that had finished when the model was asked, so nothing it couldn't see
counts in its favor. A move smaller than THRESHOLD either way is "flat": neither right nor wrong.

- buy?  buy  -> right if the price rose, wrong if it fell
- buy?  hold -> right if it fell (avoided a drop), wrong if it rose (missed a rise)
- sell? sell -> right if it fell, wrong if it rose
- sell? hold -> right if it rose, wrong if it fell

This grades the call, not the trade: a low-confidence buy that never became an order still counts.
Each call is also tagged with the indicator values the model was shown (saved with the call;
recalculated from price history for calls made before that was recorded), so the report can
show how it does in different conditions: oversold or not, above or below its long average, and so on.

Nothing here asks the model anything, so grading is free.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from taylortrader import indicators
from taylortrader.strategies.ai import FEATURES as FEATURE_KEYS

BAR = timedelta(hours=1)          # the live report card grades on hourly bars
SESSION = timedelta(hours=6.5)    # a regular trading day
THRESHOLD = 1.0         # percent
BUCKETS = ((0.0, 0.6, "Under 60%"), (0.6, 0.7, "60–69%"), (0.7, 1.01, "70% and up"))


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def horizon_bars(bar: timedelta) -> int:
    """Bars in one regular trading day. Rounded down on purpose: Alpaca stamps hourly bars on the
    hour and data.regular_hours drops the 9:00 bar (it starts before 9:30), so a day is the six bars
    from 10:00 to 15:00, and 9:30-10:00 isn't in any hourly bar."""
    return max(1, int(SESSION / bar))


HORIZON_BARS = horizon_bars(BAR)


def outcome(df: pd.DataFrame, when, n: int = HORIZON_BARS, bar: timedelta = BAR) -> dict | None:
    """Price when asked, then the close, high and low over the next `n` bars. None until known."""
    if df is None or df.empty:
        return None
    t = _ts(when)
    done = df.index + bar <= t           # bars that had finished when the model was asked
    if not done.any():
        return None
    k = int(done.nonzero()[0][-1])
    after = df.iloc[k + 1:k + 1 + n]
    if len(after) < n:
        return None
    p0 = float(df["close"].iloc[k])
    return {
        "price": p0,
        "ret": (float(after["close"].iloc[-1]) / p0 - 1) * 100,
        "up": (float(after["high"].max()) / p0 - 1) * 100,
        "down": (float(after["low"].min()) / p0 - 1) * 100,
    }


def verdict(question: str, action: str, ret: float, threshold: float = THRESHOLD) -> str:
    if abs(ret) < threshold:
        return "flat"
    wants_up = (question, action) in (("entry", "buy"), ("exit", "hold"))
    return "right" if (ret > 0) == wants_up else "wrong"


def recalculated(df: pd.DataFrame | None, when, bar: timedelta = BAR) -> dict | None:
    """Indicator values on the last finished bar, for calls saved before their values were recorded.
    Close to what the model saw, not exact: history gets revised slightly after the fact."""
    if df is None or df.empty:
        return None
    t = _ts(when)
    done = df.index + bar <= t
    if not done.any():
        return None
    k = int(done.nonzero()[0][-1])
    row = indicators.add_all(df.iloc[: k + 1]).iloc[-1].to_dict()
    return {k: round(float(v), 4) for k, v in row.items() if k in FEATURE_KEYS and pd.notna(v)}


def grade(decisions: list[dict], bars: dict[str, pd.DataFrame], threshold: float = THRESHOLD,
          bar: timedelta = BAR) -> list[dict]:
    """Every answered decision with its outcome (or verdict "pending" until the day has passed).
    `bar` is the size of the bars in `bars`."""
    out = []
    for d in decisions:
        if d.get("error") or not d.get("action"):
            continue
        o = outcome(bars.get(d["symbol"]), d["ts"], horizon_bars(bar), bar)
        row = {k: d.get(k) for k in ("id", "ts", "strategy", "symbol", "question", "action",
                                     "confidence", "acted", "summary")}
        feats = d.get("features")
        if isinstance(feats, str):
            feats = json.loads(feats)
        row["features"], row["features_saved"] = feats, bool(feats)
        if not feats:
            row["features"] = recalculated(bars.get(d["symbol"]), d["ts"], bar)
        if o is None:
            out.append({**row, "price": None, "ret": None, "up": None, "down": None, "verdict": "pending"})
        else:
            out.append({**row, **o, "verdict": verdict(d["question"], d["action"], o["ret"], threshold)})
    return out


def _avg(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _tally(rows: list[dict]) -> dict:
    right = sum(r["verdict"] == "right" for r in rows)
    wrong = sum(r["verdict"] == "wrong" for r in rows)
    return {"n": len(rows), "right": right, "wrong": wrong, "flat": len(rows) - right - wrong,
            # Share right among calls that mattered; flat days don't count either way.
            "accuracy": right / (right + wrong) * 100 if right + wrong else None,
            "avg_ret": _avg(r["ret"] for r in rows)}


MIN_SLICE = 10  # fewer calls than this in a slice is too few to read anything into

# (group, label, test on the features) - each group splits the calls one way.
SLICES = [
    ("RSI", "Under 30 (oversold)", lambda f: f["rsi"] < 30, ("rsi",)),
    ("RSI", "30 to 50", lambda f: 30 <= f["rsi"] < 50, ("rsi",)),
    ("RSI", "50 to 70", lambda f: 50 <= f["rsi"] < 70, ("rsi",)),
    ("RSI", "70 and up (overbought)", lambda f: f["rsi"] >= 70, ("rsi",)),
    ("Long trend", "Above the 200-bar average", lambda f: f["close"] > f["ma_200"], ("close", "ma_200")),
    ("Long trend", "Below the 200-bar average", lambda f: f["close"] <= f["ma_200"], ("close", "ma_200")),
    ("Short trend", "Above the 20-bar average", lambda f: f["close"] > f["ma_20"], ("close", "ma_20")),
    ("Short trend", "Below the 20-bar average", lambda f: f["close"] <= f["ma_20"], ("close", "ma_20")),
    ("MACD", "Above its signal (momentum up)", lambda f: f["macd_diff"] > 0, ("macd_diff",)),
    ("MACD", "Below its signal (momentum down)", lambda f: f["macd_diff"] <= 0, ("macd_diff",)),
    ("Money flow", "Flowing in (Chaikin above 0)", lambda f: f["chaikin"] > 0, ("chaikin",)),
    ("Money flow", "Flowing out (Chaikin 0 or below)", lambda f: f["chaikin"] <= 0, ("chaikin",)),
]


def by_indicator(done: list[dict]) -> list[dict]:
    """How the AI did in different market conditions. Every slice is counted, but a slice under
    MIN_SLICE calls is flagged so the page shows it as "not enough yet" instead of a percentage."""
    groups: dict[str, list[dict]] = {}
    for group, label, test, needs in SLICES:
        rows = [r for r in done if r.get("features") and all(k in r["features"] for k in needs)
                and test(r["features"])]
        t = _tally(rows)
        groups.setdefault(group, []).append({"label": label, **t, "enough": t["n"] >= MIN_SLICE})
    return [{"group": g, "slices": v} for g, v in groups.items()]


def report(graded: list[dict], threshold: float = THRESHOLD, bar: timedelta = BAR) -> dict:
    n = horizon_bars(bar)
    size = "hourly bar" if bar == BAR else "bar"
    done = [r for r in graded if r["verdict"] != "pending"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in done:
        groups[f"{r['question']}:{r['action']}"].append(r)
    entries = [r for r in done if r["question"] == "entry"]
    buys = groups.get("entry:buy", [])
    return {
        "horizon": f"the next trading day ({n} {size}{'' if n == 1 else 's'})",
        "threshold": threshold,
        "graded": len(done),
        "pending": len(graded) - len(done),
        "overall": _tally(done),
        "by_call": {k: _tally(v) for k, v in sorted(groups.items())},
        # The baseline: buying every time the free rule let the question through, no AI.
        "rule_only": {"n": len(entries), "avg_ret": _avg(r["ret"] for r in entries)},
        "ai_buys": {"n": len(buys), "avg_ret": _avg(r["ret"] for r in buys)},
        "confidence": [{"label": label, **_tally([r for r in done if lo <= (r["confidence"] or 0) < hi])}
                       for lo, hi, label in BUCKETS],
        "by_indicator": by_indicator(done),
        "min_slice": MIN_SLICE,
        "recalculated": sum(1 for r in done if r.get("features") and not r["features_saved"]),
        "recent": sorted(graded, key=lambda r: r["ts"], reverse=True)[:30],
    }


def bars_needed(decisions: list[dict], now: datetime) -> dict[str, datetime]:
    """Per symbol, how far back to fetch hourly bars to grade these decisions."""
    first: dict[str, datetime] = {}
    for d in decisions:
        if d.get("error") or not d.get("action"):
            continue
        t = _ts(d["ts"]).to_pydatetime() - timedelta(days=60)  # 200+ hourly bars, for recalculating ma_200
        first[d["symbol"]] = min(first.get(d["symbol"], now), t)
    return first
