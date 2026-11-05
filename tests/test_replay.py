import math
import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pandas as pd
import pytest

from taylortrader import indicators, replay
from taylortrader.llm import Decision
from taylortrader.strategies import get_strategy

T0 = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)


def frame(n=320, seed=1, base=100.0):
    rng = random.Random(seed)
    closes, p = [], base
    for i in range(n):
        p *= 1 + 0.01 * math.sin(i / 9) + rng.uniform(-0.004, 0.004)
        closes.append(p)
    idx = pd.date_range(T0, periods=n, freq="h", tz="UTC")
    c = pd.Series(closes, index=idx)
    return pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1e6})


def strategy(**kw):
    return get_strategy("ai", {"entry_when": "rsi < 50", "use_news": False, **kw})


def test_disguise_changes_prices_and_dates_but_not_the_chart():
    df = frame()
    fake = replay.disguise(df, random.Random(3))
    real = indicators.add_all(df)
    k = fake["close"].iloc[0] / df["close"].iloc[0]
    assert abs(k - 1) > 0.3
    assert (fake.index - df.index == replay.SHIFT).all()
    assert (fake.index.day_of_week == df.index.day_of_week).all()
    for col in ("rsi", "chaikin"):  # exactly what the live bot computed
        assert fake[col].dropna().values == pytest.approx(real[col].dropna().values)
    for col in ("close", "ma_200", "macd_diff"):
        assert fake[col].dropna().values == pytest.approx(real[col].dropna().values * k)


def test_questions_hide_the_ticker_follow_the_rule_and_leave_room_to_grade():
    frames = {"NVDA": frame(seed=1), "PLTR": frame(seed=2, base=170.24)}
    qs, qualified = replay.questions(frames, strategy(), bar=timedelta(hours=1), gap=7, max_calls=500, seed=1)
    assert qs and qualified == len(qs)
    for q in qs:
        assert "NVDA" not in q.prompt and "PLTR" not in q.prompt and "170.24" not in q.prompt
        assert q.alias in q.prompt and "2046" in q.prompt and "2026" not in q.prompt
        assert q.feats["rsi"] < 50
        assert replay.WARMUP <= q.i <= len(frames[q.symbol]) - 1 - 7
        assert q.ts == frames[q.symbol].index[q.i] + timedelta(hours=1)
    per = [q.i for q in qs if q.symbol == "NVDA"]
    assert all(b - a >= 7 for a, b in pairwise(per))
    few, _ = replay.questions(frames, strategy(), bar=timedelta(hours=1), gap=7, max_calls=5, seed=1)
    assert len(few) == 5 and {q.key for q in few} <= {q.key for q in qs}  # sampling reuses the same prompts


class FakeLLM:
    def __init__(self, fail_on=()):
        self.calls, self.fail_on = 0, set(fail_on)

    def ask(self, system, user, allowed):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("boom")
        rsi = float(user.split('"rsi":')[1].split(",")[0])
        return Decision("buy" if rsi < 35 else "hold", 0.8, "s", input_tokens=10, output_tokens=5)


def test_answers_are_saved_resumed_and_graded(tmp_path):
    frames = {"NVDA": frame(seed=1)}
    qs, _ = replay.questions(frames, strategy(), bar=timedelta(hours=1), gap=7, max_calls=500, seed=1)
    answers = replay.Answers(tmp_path / "a.jsonl")
    llm = FakeLLM(fail_on={1})
    replay.ask_all(qs, llm, answers, "fake", workers=1)
    assert len(answers.saved) == len(qs) - 1          # the failed call isn't saved

    again = replay.Answers(tmp_path / "a.jsonl")     # reloaded from disk
    llm2 = FakeLLM()
    replay.ask_all(qs, llm2, again, "fake", workers=1)
    assert llm2.calls == 1 and len(again.saved) == len(qs)

    rep = replay.grade(qs, again, frames, min_confidence=0.65)
    assert rep["asked"] == rep["answered"] == len(qs)
    assert rep["graded"] == len(qs)                   # every question had a full day after it
    buys = rep["by_call"].get("entry:buy", {"n": 0})["n"]
    assert rep["would_trade"]["n"] == buys
    assert rep["rule_only"]["n"] == len(qs)
    assert set(rep["by_symbol"]) == {"NVDA"}
