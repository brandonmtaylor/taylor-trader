"""A small, honest bar-by-bar backtester.

- Signals use bar i's close; fills happen at bar i+1's open (no look-ahead).
- Stops and take-profits are checked against each bar's low/high, stop first (conservative).
  A gap through the stop fills at the open, not the stop price.
- Slippage applies to every fill. Whole shares only, like live bracket orders.
- Always reported next to buy-and-hold over the same bars.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from taylortrader import indicators
from taylortrader.strategies import Bars, Strategy


@dataclass(frozen=True)
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    shares: int
    entry_reason: str
    exit_reason: str

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1) * 100


@dataclass
class Result:
    strategy: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    total_return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    buy_hold_max_drawdown_pct: float
    exposure_pct: float
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series | None = None

    @property
    def win_rate_pct(self) -> float:
        return 100 * sum(t.return_pct > 0 for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def stopped_out(self) -> int:
        return sum(t.exit_reason.startswith("stop") for t in self.trades)

    def summary(self) -> dict:
        return {
            "strategy": self.strategy,
            "period": f"{self.start.date()} to {self.end.date()}",
            "bars": self.bars,
            "return_%": round(self.total_return_pct, 2),
            "buy_hold_%": round(self.buy_hold_return_pct, 2),
            "vs_buy_hold_pts": round(self.total_return_pct - self.buy_hold_return_pct, 2),
            "max_dd_%": round(self.max_drawdown_pct, 2),
            "buy_hold_max_dd_%": round(self.buy_hold_max_drawdown_pct, 2),
            "trades": len(self.trades),
            "win_rate_%": round(self.win_rate_pct, 1),
            "stopped_out": self.stopped_out,
            "exposure_%": round(self.exposure_pct, 1),
        }


def _max_drawdown(curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(curve)
    return float(((curve - peak) / peak).min() * 100)


def run(
    df: pd.DataFrame,
    strategy: Strategy,
    allocation_pct: float = 100.0,
    stop_loss_pct: float = 12.0,
    take_profit_pct: float = 0.0,
    slippage_bps: float = 5.0,
    start_cash: float = 10_000.0,
) -> Result:
    data = indicators.add_all(df)
    b = Bars(data)
    o, h, lo, c = b.open, b.high, b.low, b.close
    n, slip, idx = len(data), slippage_bps / 10_000, data.index
    first = strategy.warmup
    if n <= first + 2:
        raise ValueError(f"need more than {first + 2} bars, got {n}")

    cash, shares = start_cash, 0
    entry_px = stop_px = tp_px = 0.0
    entry_i, entry_reason = 0, ""
    pending: tuple[str, str] | None = None  # ("buy"|"sell", reason) to fill at next open
    trades: list[Trade] = []
    curve = np.full(n, start_cash)
    in_market = 0

    def close_trade(i: int, px: float, reason: str) -> None:
        nonlocal cash, shares
        cash += shares * px
        trades.append(Trade(idx[entry_i], entry_px, idx[i], px, shares, entry_reason, reason))
        shares = 0

    for i in range(first, n):
        # 1) fill whatever was decided at the previous close, at this bar's open
        if pending:
            action, reason = pending
            pending = None
            if action == "buy" and shares == 0:
                px = o[i] * (1 + slip)
                qty = int((cash * allocation_pct / 100) // px)
                if qty > 0:
                    shares, cash = qty, cash - qty * px
                    entry_px, entry_i, entry_reason = px, i, reason
                    stop_px = px * (1 - stop_loss_pct / 100)
                    tp_px = px * (1 + take_profit_pct / 100) if take_profit_pct > 0 else np.inf
            elif action == "sell" and shares > 0:
                close_trade(i, o[i] * (1 - slip), reason)

        # 2) broker-side stop / take-profit during this bar
        if shares > 0:
            if lo[i] <= stop_px:
                close_trade(i, min(o[i], stop_px) * (1 - slip), f"stop-loss {stop_loss_pct}%")
            elif h[i] >= tp_px:
                close_trade(i, max(o[i], tp_px) * (1 - slip), f"take-profit {take_profit_pct}%")

        # 3) decide at this bar's close
        if i < n - 1:
            if shares == 0:
                sig = strategy.entry(b, i)
                if sig:
                    pending = ("buy", sig.reason)
            else:
                sig = strategy.exit(b, i, entry_px)
                if sig:
                    pending = ("sell", sig.reason)

        in_market += shares > 0
        curve[i] = cash + shares * c[i]

    if shares > 0:
        close_trade(n - 1, c[n - 1], "end of backtest")
        curve[n - 1] = cash

    curve = curve[first:]
    bh = c[first:] / o[first + 1] if n > first + 1 else c[first:] / c[first]
    return Result(
        strategy=strategy.name,
        bars=n - first,
        start=idx[first],
        end=idx[-1],
        total_return_pct=(curve[-1] / start_cash - 1) * 100,
        buy_hold_return_pct=(c[-1] / o[first + 1] - 1) * 100,
        max_drawdown_pct=_max_drawdown(curve),
        buy_hold_max_drawdown_pct=_max_drawdown(bh),
        exposure_pct=100 * in_market / (n - first),
        trades=trades,
        equity=pd.Series(curve, index=idx[first:]),
    )
