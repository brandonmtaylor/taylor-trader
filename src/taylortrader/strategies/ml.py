"""A machine-learning strategy, for research: `taylortrader search --grid ml`.

A gradient-boosted tree model (scikit-learn) guesses whether the stock will be higher `horizon` bars
from now. It buys when that chance is at least `buy_above` and sells when it falls below `sell_below`.

What keeps the backtest honest:
- Walk-forward: the model is trained only on bars whose outcome was already known at the time, and
  retrained every `retrain_every` bars as time moves on. It never sees the bars it's scored on.
- Features are ratios and returns (not raw prices), all computed from that bar and earlier ones.
- Fixed model settings. Tuning them on the same data you judge them on is how ML backtests lie.

It's research-only: it can't be picked for live or paper trading yet. The live service keeps only
about a year of bars, which is too little to train on, and nothing here has passed the search's
holdout and luck tests.

Needs the optional extra: `uv tool install 'taylortrader[ml]'`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from taylortrader import indicators
from taylortrader.strategies.base import NO, Bars, Signal, Strategy
from taylortrader.strategies.registry import register

FEATURE_WARMUP = 200  # the longest feature (distance from the 200-bar average) needs this many bars


def features(df: pd.DataFrame) -> pd.DataFrame:
    """One row per bar, from that bar and earlier ones only. Scale-free, so one model fits any price."""
    c, v = df["close"], df["volume"].astype(float)
    r = np.log(c).diff()
    ma20, ma50, ma200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    vol20 = r.rolling(20).std()
    f = pd.DataFrame(index=df.index)
    for n in (1, 5, 20, 60):
        f[f"ret_{n}"] = np.log(c / c.shift(n))
    f["vol_20"] = vol20
    f["vol_ratio"] = r.rolling(5).std() / vol20
    f["above_ma20"] = c / ma20 - 1
    f["above_ma50"] = c / ma50 - 1
    f["above_ma200"] = c / ma200 - 1
    f["ma20_vs_ma200"] = ma20 / ma200 - 1
    f["rsi"] = indicators.rsi(c, 14)
    f["macd_diff"] = indicators.macd(c)["macd_diff"] / c
    f["chaikin"] = indicators.chaikin(df, 20)
    f["atr"] = indicators.atr(df, 14) / c
    f["from_high_20"] = c / df["high"].rolling(20).max() - 1
    f["from_low_20"] = c / df["low"].rolling(20).min() - 1
    f["volume_ratio"] = v / v.rolling(20).mean()
    return f.replace([np.inf, -np.inf], np.nan)


def labels(df: pd.DataFrame, horizon: int) -> np.ndarray:
    """1 if buying at the next bar's open and selling `horizon` bars later made money; NaN while unknown.
    Row j's label needs open[j + 1 + horizon], so it's known once bar j + 1 + horizon has opened."""
    o = df["open"].to_numpy(dtype=float)
    out = np.full(len(o), np.nan)
    k = len(o) - 1 - horizon
    if k > 0:
        out[:k] = (o[1 + horizon:] > o[1:k + 1]).astype(float)
    return out


@register
class MachineLearning(Strategy):
    name = "ml"
    description = ("Machine learning (research only): a tree model, retrained as it goes on past bars only, "
                   "guesses whether the price will be higher in `horizon` bars. Buys above `buy_above`, sells "
                   "below `sell_below`.")
    research_only = True

    @dataclass(frozen=True)
    class Params:
        horizon: int = 5          # bars ahead it predicts
        buy_above: float = 0.55   # chance of being higher, to buy
        sell_below: float = 0.50  # chance falls under this: sell
        min_train: int = 500      # labelled bars needed before it trades (about two years of daily bars)
        retrain_every: int = 21   # bars between retrains (about a month of daily bars)
        max_train: int = 0        # 0 = learn from all past bars; otherwise only the newest this many

    def __init__(self, params=None):
        super().__init__(params)
        p = self.p
        if p.horizon < 1 or p.retrain_every < 1 or p.min_train < 50:
            raise ValueError("ml: horizon and retrain_every must be at least 1, min_train at least 50")
        if not 0 < p.sell_below <= p.buy_above < 1:
            raise ValueError("ml: need 0 < sell_below <= buy_above < 1")
        self._key: tuple | None = None
        self._x: np.ndarray | None = None
        self._y: np.ndarray | None = None
        self._probs: dict[int, float] = {}
        self._valid_until = -1
        self.retrains = 0

    @property
    def warmup(self) -> int:
        # Enough bars that by the first trade the model has min_train labelled rows with full features.
        return FEATURE_WARMUP + self.p.min_train + self.p.horizon + 1

    def _prepare(self, b: Bars) -> None:
        key = (id(b.df), len(b.df))
        if key != self._key:
            self._key = key
            self._x = features(b.df).to_numpy(dtype=float)
            self._y = labels(b.df, self.p.horizon)
            self._probs, self._valid_until = {}, -1

    def _fit_and_predict(self, i: int) -> None:
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            from threadpoolctl import threadpool_limits  # installed with scikit-learn
        except ImportError:
            raise ValueError("the ml strategy needs scikit-learn: uv tool install 'taylortrader[ml]' "
                             "(from source: uv sync --extra ml)") from None
        p = self.p
        last = i - 1 - p.horizon  # newest row whose outcome is known at bar i's close
        first = FEATURE_WARMUP if not p.max_train else max(FEATURE_WARMUP, last + 1 - p.max_train)
        end = min(i + p.retrain_every, len(self._x))
        self._valid_until = end - 1
        if last + 1 - first < p.min_train:
            self._probs.update({k: np.nan for k in range(i, end)})
            return
        y = self._y[first:last + 1]
        assert not np.isnan(y).any(), "label used before it was known"
        if y.min() == y.max():  # only ups (or only downs) so far: the model can't learn anything else
            self._probs.update({k: float(y[0]) for k in range(i, end)})
            return
        model = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.05, max_leaf_nodes=15,
                                               min_samples_leaf=40, l2_regularization=1.0,
                                               early_stopping=False, random_state=0)
        # One thread: at a few thousand rows, starting threads costs more than it saves (about 4x).
        with threadpool_limits(1):
            model.fit(self._x[first:last + 1], y.astype(int))
            # Features are causal, so predicting the next bars with this model uses nothing from the future.
            probs = model.predict_proba(self._x[i:end])[:, 1]
        self.retrains += 1
        self._probs.update(dict(zip(range(i, end), probs.tolist(), strict=True)))

    def prob(self, b: Bars, i: int) -> float:
        """The model's chance the price is higher in `horizon` bars, as of bar i's close."""
        self._prepare(b)
        if i > self._valid_until or i not in self._probs:
            self._fit_and_predict(i)
        return self._probs[i]

    def entry(self, b: Bars, i: int) -> Signal:
        pr = self.prob(b, i)
        if not np.isnan(pr) and pr >= self.p.buy_above:
            return Signal(True, f"model: {pr:.0%} chance of higher in {self.p.horizon} bars")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        pr = self.prob(b, i)
        if not np.isnan(pr) and pr < self.p.sell_below:
            return Signal(True, f"model: only {pr:.0%} chance of higher in {self.p.horizon} bars")
        return NO
