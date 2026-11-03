from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from typing import Any, ClassVar

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    act: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.act


NO = Signal(False)


class Bars:
    """Column access as numpy arrays (fast in backtests): bars.close[i], bars.rsi[i]."""

    def __init__(self, df: pd.DataFrame, symbol: str | None = None):
        self.df = df
        self.symbol = symbol  # set by the live service; None in backtests
        self._cols: dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.df)

    def __getattr__(self, name: str) -> np.ndarray:
        if name.startswith("_") or name in ("df", "symbol"):
            raise AttributeError(name)
        if name not in self._cols:
            if name not in self.df.columns:
                raise AttributeError(f"no column {name!r}; did you run indicators.add_all()?")
            self._cols[name] = self.df[name].to_numpy(dtype=float)
        return self._cols[name]


class Strategy(ABC):
    """A strategy decides entries and exits. It never places orders or sizes positions.

    Stops are not a strategy's job: every entry gets a stop-loss held at the broker.
    `i` is the index of the latest completed bar; never read past it.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    backtestable: ClassVar[bool] = True  # False when replaying history would be dishonest (AI)
    Params: ClassVar[type]  # a dataclass of tunable parameters with defaults

    def __init__(self, params: dict[str, Any] | None = None):
        params = dict(params or {})
        allowed = {f.name for f in fields(self.Params)}
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}; allowed: {sorted(allowed)}")
        self.p = self.Params(**params)

    @property
    def warmup(self) -> int:
        """Bars needed before signals mean anything."""
        return 35

    @abstractmethod
    def entry(self, b: Bars, i: int) -> Signal: ...

    @abstractmethod
    def exit(self, b: Bars, i: int, entry_price: float) -> Signal: ...


def ok(x: float) -> bool:
    return not np.isnan(x)
