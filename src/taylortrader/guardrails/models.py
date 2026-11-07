from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderIntent:
    """Something a strategy (or an AI agent) wants to do, before it reaches the broker."""

    symbol: str
    side: Side
    qty: float
    est_price: float
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.est_price


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    day_start_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: int = 0

    @property
    def exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values())

    @property
    def day_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity - self.day_start_equity) / self.day_start_equity * 100
