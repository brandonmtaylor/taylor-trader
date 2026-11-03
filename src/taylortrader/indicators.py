"""Technical indicators, ported from the original bot (stock_market/ticker.py) to match its values.

All indicators are causal: each value uses only that bar and earlier ones.
Input is a DataFrame with open/high/low/close/volume columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, days: int) -> pd.Series:
    return close.rolling(days).mean()


def rsi(close: pd.Series, days: int = 14) -> pd.Series:
    # Same as the original: gains/losses rounded to cents, Wilder smoothing via ewm(com=days-1).
    delta = close.diff()
    gain = delta.clip(lower=0).round(2)
    loss = delta.clip(upper=0).abs().round(2)
    avg_gain = gain.ewm(com=days - 1, min_periods=days).mean()
    avg_loss = loss.ewm(com=days - 1, min_periods=days).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1.0 + rs))


def macd(close: pd.Series) -> pd.DataFrame:
    ema_12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    line = ema_12 - ema_26
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    return pd.DataFrame({"macd": line, "macd_signal": signal, "macd_diff": line - signal})


def chaikin(df: pd.DataFrame, days: int = 20) -> pd.Series:
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    mult = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    mfv = mult * df["volume"]
    return mfv.rolling(days).mean() / df["volume"].rolling(days).mean()


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """Add every indicator the built-in strategies use. Returns a new DataFrame."""
    out = df.copy()
    out["ma_20"] = sma(out["close"], 20)
    out["ma_200"] = sma(out["close"], 200)
    out["rsi"] = rsi(out["close"], 14)
    out = out.join(macd(out["close"]))
    out["chaikin"] = chaikin(out, 20)
    return out
