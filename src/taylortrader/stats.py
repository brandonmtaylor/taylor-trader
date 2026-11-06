"""Closed-trade results from the broker's fills.

Fills come from Alpaca (the source of truth: it sees stop-loss fills the bot never placed itself).
Buys and sells are paired first-in, first-out per symbol into round trips. A sell with no buy
inside the window (the position was opened earlier) is skipped rather than guessed at.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
EPS = 1e-9


def utc_ts(v) -> pd.Timestamp:
    """Any timestamp as UTC; one with no zone is taken to be UTC already."""
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


@dataclass(frozen=True)
class Fill:
    ts: datetime
    symbol: str
    side: str  # buy | sell
    qty: float
    price: float
    order_id: str


@dataclass
class Trade:
    symbol: str
    strategy: str | None  # None: not opened by TaylorTrader (bought by hand or by the old bot)
    qty: float
    entry: float
    exit: float
    opened: datetime
    closed: datetime

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.qty

    @property
    def pct(self) -> float:
        return (self.exit / self.entry - 1) * 100 if self.entry else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        return {**d, "opened": self.opened.isoformat(), "closed": self.closed.isoformat(),
                "pnl": self.pnl, "pct": self.pct, "win": self.pnl > 0}


def round_trips(fills: list[Fill], strategy_of: dict[str, str]) -> list[Trade]:
    """Pair fills into closed trades, oldest first. `strategy_of` maps a buy's order id to its strategy."""
    lots: dict[str, deque] = defaultdict(deque)  # symbol -> [qty, price, ts, strategy]
    trades: list[Trade] = []
    for f in sorted(fills, key=lambda f: f.ts):
        if f.side == "buy":
            lots[f.symbol].append([f.qty, f.price, f.ts, strategy_of.get(f.order_id)])
            continue
        left = f.qty
        while left > EPS and lots[f.symbol]:
            lot = lots[f.symbol][0]
            take = min(left, lot[0])
            last = trades[-1] if trades else None
            if last and last.symbol == f.symbol and last.closed == f.ts and last.opened == lot[2]:
                last.qty += take  # same buy closed by the same sell: one trade, not fragments
            else:
                trades.append(Trade(f.symbol, lot[3], take, lot[1], f.price, lot[2], f.ts))
            lot[0] -= take
            left -= take
            if lot[0] <= EPS:
                lots[f.symbol].popleft()
    return trades


def summarize(trades: list[Trade], today: date) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    closed_today = [t for t in trades if t.closed.astimezone(NY).date() == today]

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    last = ["W" if t.pnl > 0 else "L" for t in trades[-5:]]
    streak = None
    if trades:
        kind = last[-1]
        n = 0
        for t in reversed(trades):
            if ("W" if t.pnl > 0 else "L") != kind:
                break
            n += 1
        streak = {"kind": kind, "n": n}
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else None,
        "avg_win": avg([t.pnl for t in wins]),
        "avg_loss": avg([t.pnl for t in losses]),
        "avg_win_pct": avg([t.pct for t in wins]),
        "avg_loss_pct": avg([t.pct for t in losses]),
        # Dollars won per dollar lost. None until there's at least one loss to divide by.
        "profit_factor": gross_win / gross_loss if gross_loss > EPS else None,
        "realized": sum(t.pnl for t in trades),
        "today": {"trades": len(closed_today), "wins": sum(1 for t in closed_today if t.pnl > 0),
                  "realized": sum(t.pnl for t in closed_today)},
        "last": last,
        "streak": streak,
    }


def results(fills: list[Fill], strategy_of: dict[str, str], now: datetime) -> dict:
    trades = round_trips(fills, strategy_of)
    today = now.astimezone(NY).date()
    bot = [t for t in trades if t.strategy]
    other = [t for t in trades if not t.strategy]
    by_strategy: dict[str, list[Trade]] = defaultdict(list)
    for t in bot:
        by_strategy[t.strategy].append(t)
    summaries = {k: summarize(v, today) for k, v in sorted(by_strategy.items())}
    return {
        "bot": summarize(bot, today),
        "by_strategy": [{"strategy": k, **{x: s[x] for x in ("trades", "wins", "win_rate", "realized")}}
                        for k, s in summaries.items()],
        "recent": [t.as_dict() for t in reversed(bot[-10:])],
        "other": {"trades": len(other), "realized": sum(t.pnl for t in other)},
    }
