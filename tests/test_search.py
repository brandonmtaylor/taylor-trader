import numpy as np
import pandas as pd
import pytest

from taylortrader import search
from taylortrader.strategies import get_strategy


def walk(n=1500, seed=0, drift=0.0002):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998,
                         "close": c, "volume": 1e6}, index=idx)


def test_rule_strategy_buys_and_sells_on_its_rules():
    s = get_strategy("rule", {"buy_when": "rsi < 40", "sell_when": "rsi > 60"})
    from taylortrader import backtest

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
                          "trades_per_bar": 0.01, "median_bars_held": 7}, {"A": df}, 0.3, seeds=5)
    b = search.luck_test({"candidate": {"strategy": "rsi_14", "params": {}, "stop_pct": 12.0},
                          "trades_per_bar": 0.01, "median_bars_held": 7}, {"A": df}, 0.3, seeds=5)
    assert a == b and len(a) == 5 and len(set(a)) > 1


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
