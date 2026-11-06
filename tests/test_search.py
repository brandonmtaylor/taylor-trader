import numpy as np
import pandas as pd
import pytest

from taylortrader import backtest, criteria, indicators, search
from taylortrader.strategies import get_strategy
from taylortrader.strategies.base import Bars


def walk(n=1500, seed=0, drift=0.0002):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998,
                         "close": c, "volume": 1e6}, index=idx)


def test_rule_strategy_buys_and_sells_on_its_rules():
    s = get_strategy("rule", {"buy_when": "rsi < 40", "sell_when": "rsi > 60"})
    r = backtest.run(walk(), s, stop_loss_pct=12)
    assert r.trades and all("rule: rsi < 40" in t.entry_reason for t in r.trades)
    assert get_strategy("rule", {"buy_when": "close > ma_200", "sell_when": ""}).warmup == 200
    with pytest.raises(ValueError, match="can't be empty"):
        get_strategy("rule", {"buy_when": "", "sell_when": "rsi > 60"})
    with pytest.raises(ValueError, match="unknown name"):
        get_strategy("rule", {"buy_when": "price < 3", "sell_when": ""})


def test_the_search_never_trades_in_the_holdout_and_the_holdout_test_only_trades_there():
    frames = {"A": walk(seed=1), "B": walk(seed=2)}
    c = search.Candidate("rule", {"buy_when": "rsi < 45", "sell_when": "rsi > 55"}, 8.0)
    for window in ("search", "holdout"):
        for df in frames.values():
            s = search.split_at(df, 0.3)
            strat = get_strategy(c.strategy, c.params)
            start, end = (strat.warmup, s) if window == "search" else (s, len(df))
            r = search._run(df, strat, c.stop_pct, start, end, 5.0)
            assert r.trades
            if window == "search":
                assert all(t.exit_time < df.index[s] for t in r.trades)
            else:
                assert all(t.entry_time > df.index[s] for t in r.trades)


def test_random_entries_are_repeatable_and_have_no_skill():
    df = walk(seed=3)
    a = search.luck_test({"candidate": {"strategy": "rsi_14", "params": {}, "stop_pct": 12.0},
                          "trades_per_bar": 0.01, "holds": [7]}, {"A": df}, 0.3, seeds=5)
    b = search.luck_test({"candidate": {"strategy": "rsi_14", "params": {}, "stop_pct": 12.0},
                          "trades_per_bar": 0.01, "holds": [7]}, {"A": df}, 0.3, seeds=5)
    assert a == b and len(a) == 5 and len(set(a)) > 1


def test_held_bars_count_like_exposure_so_random_holds_match_the_winner():
    df = walk(seed=3)
    r = backtest.run(df, search.RandomEntry({"chance": 0.05, "holds": (9,)}), stop_loss_pct=90)
    closed = [t for t in r.trades if t.exit_reason == "random"]
    assert closed and {search._bars_held(df, t) for t in closed} == {9}


@pytest.mark.parametrize("candidate", [
    search.Candidate("ma_20", {}, 12.0),     # many short holds and a few long ones: mean far above median
    search.Candidate("rsi_14", {}, 12.0),    # few long holds
    search.Candidate("rule", {"buy_when": "rsi < 50 and macd_diff > 0", "sell_when": "rsi > 68 or macd_diff < 0"}, 8.0),
])
def test_random_entries_match_the_winners_time_in_the_market_and_trade_count(monkeypatch, candidate):
    frames = {"A": walk(n=3000, seed=11), "B": walk(n=3000, seed=12)}
    held = search.evaluate(candidate, frames, "holdout", 0.3)
    assert held["exposure"] > 20  # enough time in the market for the test to mean something
    exposures, trades = [], []
    real_run = search._run

    def spy(*args, **kw):
        r = real_run(*args, **kw)
        exposures.append(r.exposure_pct)
        trades.append(len(r.trades))
        return r

    monkeypatch.setattr(search, "_run", spy)
    search.luck_test(held, frames, 0.3, seeds=30)
    assert np.mean(exposures) == pytest.approx(held["exposure"], abs=5)
    assert np.mean(trades) * len(frames) == pytest.approx(held["trades"], rel=0.25)


def test_the_luck_test_copies_the_winners_shape_on_the_holdout(monkeypatch):
    seen = []
    monkeypatch.setattr(search, "luck_test", lambda shape, *a: seen.append(shape) or [])
    grid = [search.Candidate("rsi_14", {}, 12.0)]
    rep = search.run_search({"A": walk(seed=4), "B": walk(seed=5)}, grid, seeds=1)
    assert seen == [rep["holdout"]]


def test_full_search_reports_everything_and_logs_each_holdout_look(tmp_path):
    frames = {"A": walk(seed=4), "B": walk(seed=5)}
    grid = [search.Candidate("rsi_14", {}, 12.0),
            search.Candidate("rule", {"buy_when": "rsi < 45", "sell_when": "rsi > 55"}, 8.0)]
    log = tmp_path / "looks.jsonl"
    rep = search.run_search(frames, grid, holdout=0.3, seeds=10, log_path=log)
    assert rep["tried"] == 2 and len(rep["results"]) == 2           # every candidate is reported
    assert rep["winner"]["label"] == max((r for r in rep["results"] if r["trades"] >= search.MIN_TRADES),
                                         key=lambda r: r["excess"])["label"]
    assert rep["holdout"]["trades"] > 0 and rep["luck"]["runs"] == 10
    assert rep["periods"]["search"][1] <= rep["periods"]["holdout"][0]
    assert rep["verdict"].split(":")[0] in ("PASSED", "NOT PROVEN", "FAILED", "INCONCLUSIVE")
    assert rep["previous_looks"] == 0
    again = search.run_search(frames, grid, holdout=0.3, seeds=3, log_path=log)
    assert again["previous_looks"] == 1                                # the second look is flagged


@pytest.mark.parametrize(("excess", "beat", "word"), [
    (3.0, 95.0, "PASSED"), (3.0, 60.0, "NOT PROVEN"), (-2.0, 99.0, "FAILED, with a caveat"), (-2.0, 40.0, "FAILED:"),
])
def test_verdicts(excess, beat, word):
    assert search.verdict({"excess": excess, "trades": 5}, beat).startswith(word)
    assert search.verdict({"excess": None, "trades": 0}, None).startswith("INCONCLUSIVE")


def test_a_winner_with_no_luck_test_is_not_proven():  # --seeds 0
    assert search.verdict({"excess": 3.0, "trades": 5}, None) == (
        "NOT PROVEN: beat buy-and-hold on the holdout (+3.00 pts), but no luck test was run.")


def test_default_grid_is_small_and_fixed():
    g = search.default_grid()
    assert 20 <= len(g) <= 40 and len({c.label for c in g}) == len(g)


def test_symbols_listed_later_share_the_same_holdout_dates(tmp_path):
    early, late = walk(n=1500, seed=6), walk(n=1500, seed=7).iloc[400:]
    grid = [search.Candidate("rule", {"buy_when": "rsi < 45", "sell_when": "rsi > 55"}, 8.0)]
    rep = search.run_search({"OLD": early, "NEW": late}, grid, holdout=0.3, seeds=3)
    s_new = search.split_at(late, 0.3)
    assert rep["periods"]["holdout"][0] == str(late.index[s_new].date())


def test_universes_are_fixed_lists():
    assert len(search.UNIVERSES["dow2020"]) == len(set(search.UNIVERSES["dow2020"])) == 30
    assert "BA" in search.UNIVERSES["dow2020"] and "NVDA" not in search.UNIVERSES["dow2020"]  # NVDA joined in 2024
    assert len(search.UNIVERSES["sectors"]) == 9


def _bars(n=320):
    rng = np.random.default_rng(3)
    close = 100 + rng.normal(0, 1, n).cumsum()
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": rng.integers(1000, 5000, n).astype(float)}, index=idx)


@pytest.mark.parametrize("name", ["ma_50", "ema_20", "high_20", "low_20", "change_5", "avg_volume_20", "rsi_7", "atr_14"])
def test_any_length_indicators_never_see_the_future(name):
    df = _bars()
    full = Bars(df).__getattr__(name)
    early = Bars(df.iloc[:200]).__getattr__(name)
    assert not np.isnan(full[199])
    assert full[199] == pytest.approx(early[199])


def test_any_length_indicators_have_the_right_values():
    df = _bars()
    b, i = Bars(df), 100
    assert b.ma_50[i] == pytest.approx(df["close"].iloc[i - 49:i + 1].mean())
    assert b.high_20[i] == df["high"].iloc[i - 19:i + 1].max()
    assert b.low_20[i] == df["low"].iloc[i - 19:i + 1].min()
    assert b.change_5[i] == pytest.approx((df["close"].iloc[i] / df["close"].iloc[i - 5] - 1) * 100)
    assert b.avg_volume_20[i] == pytest.approx(df["volume"].iloc[i - 19:i + 1].mean())
    assert Bars(indicators.add_all(df)).rsi[i] == pytest.approx(b.rsi_14[i])


def test_a_rule_waits_for_its_longest_indicator():
    assert get_strategy("rule", {"buy_when": "close > ma_50", "sell_when": ""}).warmup == 50
    assert get_strategy("rule", {"buy_when": "close > ma_20", "sell_when": "low < low_120"}).warmup == 120
    assert get_strategy("rule", {"buy_when": "rsi < 30", "sell_when": ""}).warmup == 35


def test_a_breakout_rule_buys_above_the_previous_high():
    df = _bars()
    df.loc[df.index[250], ["close", "high"]] = 500.0
    s = get_strategy("rule", {"buy_when": "close > prev_high_20", "sell_when": ""})
    b = Bars(indicators.add_all(df))
    assert s.entry(b, 250)
    assert not s.entry(b, 251)  # the spike is now part of the recent high


@pytest.mark.parametrize("bad", ["ma_0 > 1", "ma_251 > 1", "ma_050 > 1", "prev_prev_ma_50 > 1", "ma_ > 1", "volatility_20 > 1"])
def test_rules_refuse_lengths_and_names_they_cant_compute(bad):
    with pytest.raises(criteria.CriteriaError):
        criteria.parse(bad)


def test_ai_filter_rules_can_use_any_length():
    tree = criteria.parse("close > ma_50 * 0 and prev_atr_14 > 0")
    b = Bars(indicators.add_all(_bars()))
    assert criteria.evaluate(tree, *criteria.rows(b, 100, tree))
    assert not criteria.evaluate(tree, *criteria.rows(b, 10, tree))  # not enough bars yet: no, not an error
