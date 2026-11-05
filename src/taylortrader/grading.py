"""Grade the AI's decisions against what the price did next.

Each decision is scored on the next trading day (7 hourly bars) after it was made, starting from
the close of the last bar that had finished when the model was asked, so nothing it couldn't see
counts in its favor. A move smaller than THRESHOLD either way is "flat": neither right nor wrong.

- buy?  buy  -> right if the price rose, wrong if it fell
- buy?  hold -> right if it fell (avoided a drop), wrong if it rose (missed a rise)
- sell? sell -> right if it fell, wrong if it rose
- sell? hold -> right if it rose, wrong if it fell

This grades the call, not the trade: a low-confidence buy that never became an order still counts.
Nothing here asks the model anything, so grading is free.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

HORIZON_BARS = 7        # hourly bars: about one trading day
BAR = timedelta(hours=1)
THRESHOLD = 1.0         # percent
BUCKETS = ((0.0, 0.6, "Under 60%"), (0.6, 0.7, "60–69%"), (0.7, 1.01, "70% and up"))


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def outcome(df: pd.DataFrame, when, n: int = HORIZON_BARS) -> dict | None:
    """Price when asked, then the close, high and low over the next `n` bars. None until known."""
    if df is None or df.empty:
        return None
    t = _ts(when)
    done = df.index + BAR <= t           # bars that had finished when the model was asked
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


def grade(decisions: list[dict], bars: dict[str, pd.DataFrame], threshold: float = THRESHOLD) -> list[dict]:
    """Every answered decision with its outcome (or verdict "pending" until the day has passed)."""
    out = []
    for d in decisions:
        if d.get("error") or not d.get("action"):
            continue
        o = outcome(bars.get(d["symbol"]), d["ts"])
        row = {k: d.get(k) for k in ("id", "ts", "strategy", "symbol", "question", "action",
                                     "confidence", "acted", "summary")}
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


def report(graded: list[dict], threshold: float = THRESHOLD) -> dict:
    done = [r for r in graded if r["verdict"] != "pending"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in done:
        groups[f"{r['question']}:{r['action']}"].append(r)
    entries = [r for r in done if r["question"] == "entry"]
    buys = groups.get("entry:buy", [])
    return {
        "horizon": "the next trading day (7 hourly bars)",
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
        "recent": sorted(graded, key=lambda r: r["ts"], reverse=True)[:30],
    }


def bars_needed(decisions: list[dict], now: datetime) -> dict[str, datetime]:
    """Per symbol, how far back to fetch hourly bars to grade these decisions."""
    first: dict[str, datetime] = {}
    for d in decisions:
        if d.get("error") or not d.get("action"):
            continue
        t = _ts(d["ts"]).to_pydatetime() - timedelta(days=3)
        first[d["symbol"]] = min(first.get(d["symbol"], now), t)
    return first
