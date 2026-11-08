import numpy as np
import pandas as pd
import pytest

from taylortrader import backtest, search
from taylortrader.config import ConfigError, _parse_config
from taylortrader.strategies import Bars, get_strategy, list_strategies
from taylortrader.strategies.ml import features, labels

pytest.importorskip("sklearn")


def daily(n=900, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.005,
                         "low": np.minimum(open_, close) * 0.995, "close": close,
                         "volume": rng.integers(1e5, 1e6, n).astype(float)}, index=idx)


def small(**kw):
    return get_strategy("ml", {"min_train": 100, "retrain_every": 40, **kw})


def test_features_and_labels_never_use_later_bars():
    df = daily()
    later = df.copy()
    later.iloc[600:] *= 3  # change everything after bar 599
    pd.testing.assert_frame_equal(features(df).iloc[:600], features(later).iloc[:600])
    y = labels(df, 5)
    assert np.isnan(y[-6:]).all() and not np.isnan(y[:-6]).any()
    # Row j's label compares open[j+1] with open[j+6]: unaffected by bars after j+6.
    assert (labels(later, 5)[:594] == y[:594]).all()


def test_predictions_dont_change_when_the_future_does():
    df = daily()
    later = df.copy()
    later.iloc[651:] = later.iloc[651:] * np.linspace(0.5, 2, len(later) - 651)[:, None]
    a, b = small(), small()
    pa = [a.prob(Bars(df), i) for i in range(a.warmup, 651)]
    pb = [b.prob(Bars(later), i) for i in range(b.warmup, 651)]
    assert pa == pb and a.retrains > 0


def test_it_backtests():
    r = backtest.run(daily(), small(), stop_loss_pct=8)
    assert r.bars > 0 and r.trades and r.start >= daily().index[small().warmup]


def test_params_are_checked():
    with pytest.raises(ValueError):
        get_strategy("ml", {"buy_above": 0.4, "sell_below": 0.5})


def test_research_only_never_trades():
    assert "ml" not in {s["name"] for s in list_strategies()}
    assert "ml" in {s["name"] for s in list_strategies(include_research=True)}
    with pytest.raises(ConfigError, match="research only"):
        _parse_config({"strategies": [{"name": "ml", "symbols": ["SPY"]}]})


def test_ml_grid_runs_through_the_search():
    grid = [search.Candidate("ml", {"horizon": 5, "buy_above": 0.55, "min_train": 100, "retrain_every": 60}, 8.0)]
    frames = {"A": daily(seed=1), "B": daily(seed=2)}
    rep = search.run_search(frames, grid, holdout=0.3, seeds=5)
    assert rep["results"][0]["label"].startswith("ml:")
    assert rep["verdict"]
    assert len(search.default_grid(kind="ml")) == len(search.ML) * len(search.STOPS)
    assert len(search.default_grid(kind="all")) == len(search.default_grid()) + len(search.default_grid(kind="ml"))
