"""Strategies ported from the original bot (stock_market/strategy.py).

Thresholds and logic match the original's live path (test=False) unless a docstring says otherwise.
The original's -12% stop check is gone from here: stops are now broker-side bracket legs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from taylortrader.strategies.base import NO, Bars, Signal, Strategy, ok
from taylortrader.strategies.registry import register

# --- shared building blocks -------------------------------------------------------------------

def _macd_cross_up(b: Bars, i: int, legacy: bool) -> bool:
    d, s = b.macd_diff, b.macd_signal
    if i < 2 or not (ok(d[i]) and ok(s[i])):
        return False
    # Original (live path) compared bar i-2 twice and never looked at i-1. legacy=True keeps that.
    prev = d[i - 2] <= 0 if legacy else (d[i - 1] <= 0 or d[i - 2] <= 0)
    return d[i] > 0 and s[i] <= 0 and prev


def _macd_cross_down(b: Bars, i: int, legacy: bool) -> bool:
    d, s = b.macd_diff, b.macd_signal
    if i < 2 or not (ok(d[i]) and ok(s[i])):
        return False
    prev = d[i - 2] >= 0 if legacy else (d[i - 1] >= 0 or d[i - 2] >= 0)
    return d[i] < 0 and s[i] >= 0 and prev


def _cross_above(b: Bars, i: int, ma: np.ndarray) -> bool:
    return i >= 1 and ok(ma[i]) and ok(ma[i - 1]) and b.close[i] > ma[i] and b.close[i - 1] <= ma[i - 1]


def _cross_below(b: Bars, i: int, ma: np.ndarray) -> bool:
    return i >= 1 and ok(ma[i]) and ok(ma[i - 1]) and b.close[i] < ma[i] and b.close[i - 1] >= ma[i - 1]


# --- strategies -------------------------------------------------------------------------------

@register
class MacdRsi(Strategy):
    name = "macd_rsi"
    description = (
        "Buy when MACD crosses above its signal line below zero "
        "and RSI dipped under rsi_buy in the recent window. Sell when RSI rises above rsi_sell."
    )

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 35.5
        rsi_sell: float = 68.0
        rsi_lookback_bars: int = 18  # original: 90 minutes of 5-minute bars
        legacy_crossover: bool = True

    def entry(self, b: Bars, i: int) -> Signal:
        lo = max(0, i - self.p.rsi_lookback_bars + 1)
        window = b.rsi[lo : i + 1]
        if np.all(np.isnan(window)):
            return NO
        min_rsi = np.nanmin(window)
        if min_rsi <= self.p.rsi_buy and _macd_cross_up(b, i, self.p.legacy_crossover):
            return Signal(True, f"MACD crossed up below zero; RSI hit {min_rsi:.1f} in last {len(window)} bars")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        if ok(b.rsi[i]) and b.rsi[i] > self.p.rsi_sell:
            return Signal(True, f"RSI {b.rsi[i]:.1f} > {self.p.rsi_sell}")
        return NO


@register
class Macd(Strategy):
    name = "macd"
    description = "Buy on MACD crossing above its signal line below zero. Sell on the reverse cross above zero, or RSI > 80."

    @dataclass(frozen=True)
    class Params:
        rsi_sell: float = 80.0
        legacy_crossover: bool = True

    def entry(self, b: Bars, i: int) -> Signal:
        if _macd_cross_up(b, i, self.p.legacy_crossover):
            return Signal(True, "MACD crossed up below zero")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        if ok(b.rsi[i]) and b.rsi[i] > self.p.rsi_sell:
            return Signal(True, f"RSI {b.rsi[i]:.1f} > {self.p.rsi_sell}")
        if _macd_cross_down(b, i, self.p.legacy_crossover):
            return Signal(True, "MACD crossed down above zero")
        return NO


@register
class Ma20(Strategy):
    name = "ma_20"
    description = "Buy when price crosses above its 20-bar moving average; sell when it crosses back below."

    @dataclass(frozen=True)
    class Params:
        pass

    def entry(self, b: Bars, i: int) -> Signal:
        return Signal(True, "price crossed above MA20") if _cross_above(b, i, b.ma_20) else NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        return Signal(True, "price crossed below MA20") if _cross_below(b, i, b.ma_20) else NO


@register
class Rsi14(Strategy):
    name = "rsi_14"
    description = "Buy when 14-bar RSI is under rsi_buy (oversold); sell when it's over rsi_sell."

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 35.5
        rsi_sell: float = 68.0

    def entry(self, b: Bars, i: int) -> Signal:
        r = b.rsi[i]
        return Signal(True, f"RSI {r:.1f} < {self.p.rsi_buy}") if ok(r) and r < self.p.rsi_buy else NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        r = b.rsi[i]
        return Signal(True, f"RSI {r:.1f} > {self.p.rsi_sell}") if ok(r) and r > self.p.rsi_sell else NO


@register
class Ma20Rsi(Strategy):
    name = "ma_20_rsi"
    description = "Buy on a cross above MA20 or RSI < rsi_buy; sell on a cross below MA20 or RSI > rsi_sell."

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 35.5
        rsi_sell: float = 68.0

    def entry(self, b: Bars, i: int) -> Signal:
        if _cross_above(b, i, b.ma_20):
            return Signal(True, "price crossed above MA20")
        if ok(b.rsi[i]) and b.rsi[i] < self.p.rsi_buy:
            return Signal(True, f"RSI {b.rsi[i]:.1f} < {self.p.rsi_buy}")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        if _cross_below(b, i, b.ma_20):
            return Signal(True, "price crossed below MA20")
        if ok(b.rsi[i]) and b.rsi[i] > self.p.rsi_sell:
            return Signal(True, f"RSI {b.rsi[i]:.1f} > {self.p.rsi_sell}")
        return NO


@register
class Ma200Rsi(Strategy):
    name = "ma_200_rsi"
    description = "Buy when price is above MA200 and RSI < rsi_buy; sell when price drops below MA200 or RSI > rsi_sell."

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 30.0
        rsi_sell: float = 40.0

    @property
    def warmup(self) -> int:
        return 200

    def entry(self, b: Bars, i: int) -> Signal:
        ma, r = b.ma_200[i], b.rsi[i]
        if ok(ma) and ok(r) and b.close[i] > ma and r < self.p.rsi_buy:
            return Signal(True, f"above MA200 with RSI {r:.1f}")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        ma, r = b.ma_200[i], b.rsi[i]
        if ok(ma) and b.close[i] < ma:
            return Signal(True, "price below MA200")
        if ok(r) and r > self.p.rsi_sell:
            return Signal(True, f"RSI {r:.1f} > {self.p.rsi_sell}")
        return NO


@register
class Ma20Over200Rsi(Strategy):
    name = "ma_20_over_200_rsi"
    description = "Buy in an uptrend (MA20 >= MA200) when RSI < rsi_buy; sell when RSI > rsi_sell."

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 35.5
        rsi_sell: float = 68.0

    @property
    def warmup(self) -> int:
        return 200

    def entry(self, b: Bars, i: int) -> Signal:
        m20, m200, r = b.ma_20[i], b.ma_200[i], b.rsi[i]
        if ok(m20) and ok(m200) and ok(r) and m20 >= m200 and r < self.p.rsi_buy:
            return Signal(True, f"uptrend with RSI {r:.1f}")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        r = b.rsi[i]
        return Signal(True, f"RSI {r:.1f} > {self.p.rsi_sell}") if ok(r) and r > self.p.rsi_sell else NO


@register
class Chaikin(Strategy):
    name = "chaikin"
    description = (
        "Buy when RSI < rsi_buy, or Chaikin money flow is above "
        "buy_threshold and rising. Sell on a cross below MA20, or Chaikin falling through sell_threshold."
    )

    @dataclass(frozen=True)
    class Params:
        rsi_buy: float = 35.5
        buy_threshold: float = 0.1
        sell_threshold: float = -0.1

    def entry(self, b: Bars, i: int) -> Signal:
        r, c = b.rsi[i], b.chaikin
        if ok(r) and r < self.p.rsi_buy:
            return Signal(True, f"RSI {r:.1f} < {self.p.rsi_buy}")
        if i >= 1 and ok(c[i]) and ok(c[i - 1]) and c[i] > self.p.buy_threshold and c[i] - c[i - 1] > 0:
            return Signal(True, f"Chaikin {c[i]:.2f} rising")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        c = b.chaikin
        if _cross_below(b, i, b.ma_20):
            return Signal(True, "price crossed below MA20")
        if i >= 1 and ok(c[i]) and ok(c[i - 1]) and c[i] < self.p.sell_threshold < c[i - 1]:
            return Signal(True, f"Chaikin fell through {self.p.sell_threshold}")
        return NO


@register
class Pullback(Strategy):
    name = "pullback"
    description = (
        "Three rising bars on rising volume, a two-of-three dip on lighter volume, then two rising bars: buy. "
        "Sell when RSI > rsi_sell."
    )

    @dataclass(frozen=True)
    class Params:
        rsi_sell: float = 68.0

    def entry(self, b: Bars, i: int) -> Signal:
        if i < 7:
            return NO
        c, v = b.close[i - 7 : i + 1], b.volume[i - 7 : i + 1]
        run_up = np.all(np.diff(c[0:3]) > 0) and np.sum(np.diff(v[0:3]) > 0) >= 2
        dip = np.sum(np.diff(c[3:6]) < 0) >= 2 and np.sum(v[3:6] < v[0:3].mean()) >= 2
        resume = np.all(np.diff(c[6:8]) > 0) and np.all(v[6:8] > 0)
        return Signal(True, "run-up, light-volume dip, resumption") if run_up and dip and resume else NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        r = b.rsi[i]
        return Signal(True, f"RSI {r:.1f} > {self.p.rsi_sell}") if ok(r) and r > self.p.rsi_sell else NO
