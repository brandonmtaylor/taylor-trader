"""Ask the AI strategy about past bars and grade its answers, without waiting for live calls.

The catch with asking a model about history is that it may have read what happened next. So each
stock is disguised before the model sees it:
- the ticker becomes "Stock A", "Stock B", ...
- prices are multiplied by a random factor and volume by another (RSI, Chaikin and the shape of
  every chart are unchanged; the price levels a model could recognize are gone)
- dates move forward a whole number of years' worth of weeks, so weekdays stay weekdays and the
  clock time in New York stays the same
- no news (there's no honest way to replay it), so this tests the AI on price data alone

Each disguised prompt goes through the same prompt builder and model as the live strategy, and
answers are graded exactly like the live report card (grading.py). Only buy questions are
replayed: sell questions depend on a position the replay never really held.

Answers are saved as they arrive, so a run can be stopped and resumed without asking again.

A rule with an absolute price level (close > 50) doesn't replay faithfully: rules are checked on the
disguised prices, so the level means something different for each stock. Compare prices with each
other (close > ma_200) or use ratios like RSI instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from taylortrader import criteria, grading, indicators
from taylortrader.strategies.ai import SYSTEM, AIStrategy, features
from taylortrader.strategies.base import Bars

log = logging.getLogger(__name__)

# Columns measured in dollars, scaled with the price. RSI and Chaikin are ratios and stay as they are.
PRICE_COLS = ("open", "high", "low", "close", "ma_20", "ma_200", "macd", "macd_signal", "macd_diff")
SHIFT = timedelta(weeks=52 * 20)   # ~20 years ahead; whole weeks keep the weekday
NY = "America/New_York"
WARMUP = 200                      # bars before the first question, so the 200-bar average exists


@dataclass(frozen=True)
class Question:
    symbol: str      # the real ticker (never shown to the model)
    alias: str       # what the model sees
    i: int           # bar index in the symbol's frame
    ts: pd.Timestamp  # when the live bot would have asked: the bar's end
    prompt: str
    feats: dict

    @property
    def key(self) -> str:
        h = hashlib.sha256(self.prompt.encode()).hexdigest()[:16]
        return f"{self.symbol}|{self.ts.isoformat()}|{h}"


def disguise(df: pd.DataFrame, rng: random.Random) -> pd.DataFrame:
    """Same chart, different numbers and dates. Indicators are calculated on the real prices first
    (RSI rounds moves to the cent, so it isn't quite scale-free), so the model sees exactly the
    RSI, Chaikin and crossings the live bot would have seen."""
    out = indicators.add_all(df[["open", "high", "low", "close", "volume"]])
    price_k = rng.uniform(0.15, 0.6) if rng.random() < 0.5 else rng.uniform(1.7, 6.0)
    cols = [c for c in PRICE_COLS if c in out.columns]
    out[cols] = out[cols] * price_k
    out["volume"] = (out["volume"] * rng.uniform(0.3, 3.0)).round()
    # Shifted on the New York clock: the same shift in UTC lands on the other side of a daylight
    # saving change some weeks, and 10:00 would show as 9:00 or 11:00. Stocks only trade on weekdays,
    # which never hit the hour a clock change skips or repeats.
    local = out.index.tz_convert(NY).tz_localize(None) + SHIFT
    out.index = local.tz_localize(NY).tz_convert(out.index.tz)
    return out


def _alias(n: int) -> str:
    """Stock A ... Stock Z, then Stock AA, AB, ..."""
    letters = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(ord("A") + r) + letters
    return f"Stock {letters}"


def questions(frames: dict[str, pd.DataFrame], strategy: AIStrategy, *, bar: timedelta, gap: int,
              max_calls: int, seed: int) -> tuple[list[Question], int]:
    """The buy questions to ask: bars where the free rule passes, at least `gap` bars apart per
    stock, with a full grading horizon after them. Returns (sampled, how many qualified)."""
    rng = random.Random(seed)
    rule = strategy._entry_rule
    picked: list[tuple[str, str, pd.DataFrame, int]] = []
    for n, (symbol, real) in enumerate(sorted(frames.items())):
        alias = _alias(n)
        fake = disguise(real, rng)
        bars = Bars(fake, alias)
        last = len(fake) - 1 - grading.horizon_bars(bar)
        next_ok = WARMUP
        for i in range(WARMUP, last + 1):
            if i < next_ok:
                continue
            if criteria.evaluate(rule, *criteria.rows(bars, i, rule)):
                picked.append((symbol, alias, fake, i))
                next_ok = i + gap
    qualified = len(picked)
    if len(picked) > max_calls:
        picked = rng.sample(picked, max_calls)
    out = []
    for symbol, alias, fake, i in sorted(picked, key=lambda p: (frames[p[0]].index[p[3]], p[0])):
        prompt = strategy.prompt(Bars(fake, alias), i, alias, "entry", None)
        out.append(Question(symbol, alias, i, frames[symbol].index[i] + bar, prompt, features(fake.iloc[i].to_dict())))
    return out, qualified


class Answers:
    """Saved answers, one JSON object per line, so runs resume instead of asking twice."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.saved: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    a = json.loads(line)
                    self.saved[a["key"]] = a

    def add(self, answer: dict) -> None:
        with self._lock:
            self.saved[answer["key"]] = answer
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(answer) + "\n")


def ask_all(qs: list[Question], llm, answers: Answers, model: str, *, workers: int = 2,
            on_answer: Callable[[int, int, dict], None] | None = None) -> None:
    todo = [q for q in qs if q.key not in answers.saved]
    done = 0

    def one(q: Question) -> dict:
        base = {"key": q.key, "symbol": q.symbol, "ts": q.ts.isoformat(), "model": model}
        try:
            d = llm.ask(SYSTEM, q.prompt, ("buy", "hold"))
        except Exception as e:  # noqa: BLE001 - a failed call is recorded and skipped in grading
            return {**base, "error": f"{type(e).__name__}: {e}"[:300]}
        return {**base, "action": d.action, "confidence": d.confidence, "summary": d.summary,
                "input_tokens": d.input_tokens, "output_tokens": d.output_tokens, "latency_ms": d.latency_ms}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, q) for q in todo]
        for f in as_completed(futures):
            a = f.result()
            # Failed calls aren't saved, so a rerun tries them again.
            if not a.get("error"):
                answers.add(a)
            done += 1
            if on_answer:
                on_answer(done, len(todo), a)


def grade(qs: list[Question], answers: Answers, frames: dict[str, pd.DataFrame], min_confidence: float,
          bar: timedelta = grading.BAR) -> dict:
    """The live report card's numbers, plus how the buys the bot would really have placed did."""
    decisions = []
    for n, q in enumerate(qs):
        a = answers.saved.get(q.key)
        if not a:
            continue
        decisions.append({"id": n, "ts": q.ts.isoformat(), "strategy": "replay", "symbol": q.symbol,
                          "question": "entry", "action": a["action"], "confidence": a["confidence"],
                          "acted": a["action"] == "buy" and a["confidence"] >= min_confidence,
                          "summary": a.get("summary"), "features": q.feats})
    graded = grading.grade(decisions, frames, bar=bar)
    rep = grading.report(graded, bar=bar)
    done = [r for r in graded if r["verdict"] != "pending"]
    placed = [r for r in done if r["acted"]]
    rep["would_trade"] = {"n": len(placed), "avg_ret": grading._avg(r["ret"] for r in placed),
                          "min_confidence": min_confidence}
    rep["by_symbol"] = {s: grading._tally([r for r in done if r["symbol"] == s])
                        | {"buys": sum(r["action"] == "buy" for r in done if r["symbol"] == s)}
                        for s in sorted({r["symbol"] for r in done})}
    rep["asked"], rep["answered"] = len(qs), len(decisions)
    return rep
