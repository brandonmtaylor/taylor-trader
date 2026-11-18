"""Demo mode: the dashboard on a made-up paper account (`taylortrader dashboard --demo`).

For screenshots, trying the dashboard before setting anything up, and showing it to someone.
Nothing here touches the network, Alpaca, the Keychain or your settings:
- prices are a seeded random walk (same seed, same prices), with intraday bars that add up to
  the daily ones, so charts, backtests, the what-if and the AI report card all have data;
- the broker is a stand-in that only records what you ask it to do;
- the settings and journal live in a temporary folder that's thrown away afterwards.

All numbers are made up. None of it is a forecast or a record of real trades.
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from taylortrader.config import AIConfig, AlertsConfig, Config, InvestConfig, StrategyConfig, save_config
from taylortrader.guardrails import AccountSnapshot, Limits, Position
from taylortrader.journal import Journal
from taylortrader.stats import Fill

NY = ZoneInfo("America/New_York")
SEED = 9  # picked because its made-up year looks ordinary: some wins, some losses, no miracle
FIRST_DAY = date(2013, 1, 2)
LAST_DAY = date(2031, 12, 31)
ANCHOR = date(2026, 11, 2)  # prices are scaled so this day's close is PROFILE[symbol].price
START_CASH = 10_000.0
MINUTES = 390  # 9:30 to 16:00

# Made-up price paths with roughly the character of each ticker: (price on ANCHOR, yearly drift, yearly
# volatility, beta to the market, first trading day). Not real prices.
@dataclass(frozen=True)
class Profile:
    price: float
    drift: float
    vol: float
    beta: float
    since: date = FIRST_DAY
    volume: float = 2e6


PROFILE = {
    "SPY": Profile(668.0, 0.11, 0.17, 1.0, volume=7e7),
    "VTI": Profile(331.0, 0.11, 0.18, 1.02, volume=4e6),
    "VXUS": Profile(72.4, 0.06, 0.17, 0.85, volume=4e6),
    "BND": Profile(74.1, 0.025, 0.06, 0.05, volume=6e6),
    "VT": Profile(136.5, 0.09, 0.17, 0.95, volume=2e6),
    "SGOV": Profile(100.6, 0.04, 0.004, 0.0, since=date(2020, 6, 1), volume=5e6),
    "VRT": Profile(182.0, 0.32, 0.50, 1.5, since=date(2018, 8, 2), volume=6e6),
    "NVDA": Profile(191.0, 0.42, 0.48, 1.6, volume=2e8),
    "AMD": Profile(238.0, 0.26, 0.50, 1.5, volume=5e7),
    "PLTR": Profile(176.0, 0.45, 0.60, 1.6, since=date(2020, 9, 30), volume=6e7),
    "MSFT": Profile(512.0, 0.17, 0.25, 1.1, volume=2e7),
    "AAPL": Profile(268.0, 0.16, 0.27, 1.1, volume=5e7),
    "GOOGL": Profile(281.0, 0.18, 0.29, 1.15, volume=3e7),
}

STRATEGIES = [
    StrategyConfig(name="macd_rsi", symbols=["VRT"], timeframe="5Min", allocation_pct=25, stop_loss_pct=8,
                   params={"rsi_buy": 38, "rsi_sell": 70}),
    StrategyConfig(name="ai", symbols=["NVDA", "AMD", "PLTR"], timeframe="1Hour", allocation_pct=15,
                   stop_loss_pct=8, take_profit_pct=15,
                   params={"min_confidence": 0.65}),
    StrategyConfig(name="ma_20_rsi", symbols=["MSFT", "AAPL"], timeframe="1Day", allocation_pct=10,
                   stop_loss_pct=8),
]
INVEST = InvestConfig(enabled=True, preview=False, amount=200.0, every="month", day=1, start="2026-03-01",
                      holdings=[{"symbol": "VTI", "weight": 55.0}, {"symbol": "VXUS", "weight": 25.0},
                                {"symbol": "BND", "weight": 20.0}])
# Held now, and how many trading days before the latest session each was bought (0: that session).
STILL_OPEN = {"VRT": range(3, 8), "NVDA": range(1, 4), "AMD": range(2)}
# Closed on the latest session, the one the daily email is about: two wins and a loss.
LAST_SESSION_CLOSES = ("PLTR", "MSFT", "AAPL")  # the first one whose prices allow it takes the loss
TRADES = {"VRT": 8, "NVDA": 4, "AMD": 4, "PLTR": 3, "MSFT": 2, "AAPL": 2}  # earlier closed round trips
WIN_RATE = 0.52

# What the Settings page shows instead of this machine's real Keychain, launchd and paths.
SECRETS = {"ALPACA_PAPER_KEY_ID": "Keychain", "ALPACA_PAPER_SECRET_KEY": "Keychain", "NTFY_TOPIC": "Keychain",
           "EMAIL_PASSWORD": "Keychain", "ANTHROPIC_API_KEY": "Keychain"}
SERVICE = {"supported": True, "installed": True, "loaded": True, "running": True, "pid": 4242}
MCP = {"claude_code": "claude mcp add taylortrader --scope user -- taylortrader mcp",
       "desktop_config": json.dumps({"mcpServers": {"taylortrader": {"command": "taylortrader", "args": ["mcp"]}}},
                                    indent=2)}
ALERT_NOTES = ["Demo mode: alerts are switched off, nothing is sent"]
NOT_IN_DEMO = "Not in demo mode: nothing here is connected to Alpaca, the Keychain or the background service."


def _rng(*keys: int) -> np.random.Generator:
    return np.random.default_rng([SEED, *keys])


def _key(symbol: str) -> int:
    return zlib.crc32(symbol.encode())


def profile(symbol: str) -> Profile:
    if symbol in PROFILE:
        return PROFILE[symbol]
    r = _rng(_key(symbol), 1)  # any other ticker: a plausible stock of its own
    return Profile(round(float(r.uniform(20, 300)), 2), 0.08, float(r.uniform(0.25, 0.45)), 1.1)


def _utc(when) -> pd.Timestamp:
    t = pd.Timestamp(when)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# --- prices ----------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _days() -> pd.DatetimeIndex:
    return pd.bdate_range(FIRST_DAY, LAST_DAY)


@lru_cache(maxsize=1)
def _market() -> np.ndarray:
    """The market's daily shocks, shared by every symbol so they move together like real stocks do."""
    return _rng(0).standard_t(5, len(_days())) / math.sqrt(5 / 3)


@lru_cache(maxsize=64)
def _daily(symbol: str) -> pd.DataFrame:
    """Daily bars, stamped at midnight New York time like Alpaca's."""
    p = profile(symbol)
    days = _days()
    r = _rng(_key(symbol), 2)
    n = len(days)
    spy = PROFILE["SPY"].vol
    idio = math.sqrt(max(p.vol ** 2 - (p.beta * spy) ** 2, (0.15 * p.vol) ** 2))
    shocks = p.beta * spy * _market() + idio * r.standard_t(5, n) / math.sqrt(5 / 3)
    rets = (p.drift - p.vol ** 2 / 2) / 252 + shocks / math.sqrt(252)
    gaps = r.normal(0, 0.25 * p.vol / math.sqrt(252), n)  # overnight part of each day's move
    log_close = np.cumsum(rets)
    anchor = int(days.searchsorted(pd.Timestamp(ANCHOR)))
    log_close += math.log(p.price) - log_close[anchor]
    close = np.exp(log_close)
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev * np.exp(gaps)
    wick = np.abs(r.normal(0, 0.35 * p.vol / math.sqrt(252), (2, n)))
    df = pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * np.exp(wick[0]),
        "low": np.minimum(open_, close) * np.exp(-wick[1]), "close": close,
        "volume": np.round(p.volume * np.exp(r.normal(0, 0.3, n))),
    }, index=days.tz_localize(NY).tz_convert("UTC"))
    df.index.name = "timestamp"
    return df[df.index >= pd.Timestamp(p.since, tz=NY)]


@lru_cache(maxsize=4096)
def _minutes(symbol: str, day: date) -> tuple[int, np.ndarray]:
    """One day's 390 one-minute bars, from that day's open to its close: (9:30 in ns, rows of OHLCV)."""
    d = _daily(symbol)
    stamp = pd.Timestamp(day, tz=NY)
    row = d.loc[stamp.tz_convert("UTC")]
    p = profile(symbol)
    r = _rng(_key(symbol), 3, day.toordinal())
    sigma = p.vol / math.sqrt(252)
    steps = r.normal(0, sigma / math.sqrt(MINUTES), MINUTES).cumsum()
    k = np.arange(1, MINUTES + 1) / MINUTES
    bridge = steps - k * steps[-1]
    o, c = math.log(row["open"]), math.log(row["close"])
    closes = np.exp(o + k * (c - o) + bridge)
    opens = np.concatenate([[row["open"]], closes[:-1]])
    wick = np.abs(r.normal(0, 0.6 * sigma / math.sqrt(MINUTES), (2, MINUTES)))
    u = np.abs(np.linspace(-1, 1, MINUTES)) + 0.5  # busier at the open and the close
    vol = np.round(p.volume / MINUTES * u * np.exp(r.normal(0, 0.4, MINUTES)))
    rows = np.column_stack([opens, np.maximum(opens, closes) * np.exp(wick[0]),
                            np.minimum(opens, closes) * np.exp(-wick[1]), closes, vol])
    start = int((stamp + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC").value)
    return start, rows


BAR_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60}


def _aggregate(rows: np.ndarray, size: int) -> np.ndarray:
    g = rows.reshape(-1, size, 5)
    return np.column_stack([g[:, 0, 0], g[:, :, 1].max(1), g[:, :, 2].min(1), g[:, -1, 3], g[:, :, 4].sum(1)])


class DemoMarketData:
    """Made-up bars for any ticker and bar size. Same seed, same prices: nothing is downloaded."""

    def __init__(self, now=lambda: datetime.now(UTC)):
        self.now = now

    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None, **_) -> pd.DataFrame:
        now = _utc(self.now())
        end_ts = min(_utc(end), now) if end is not None else now
        start_ts = _utc(start)
        daily = _daily(symbol.upper())
        if timeframe == "1Day":
            done = daily.index + pd.Timedelta(hours=16) <= end_ts  # a day's bar once its close is in
            return daily[done & (daily.index >= start_ts.normalize() - pd.Timedelta(hours=5))].copy()
        if timeframe not in BAR_MINUTES:
            raise ValueError(f"unsupported timeframe {timeframe}")
        size = BAR_MINUTES[timeframe]
        days = daily.index[(daily.index >= start_ts - pd.Timedelta(days=1)) & (daily.index <= end_ts)]
        stamps, blocks = [], []
        for ts in days:
            first, rows = _minutes(symbol.upper(), ts.tz_convert(NY).date())
            if size == 60:  # Alpaca's hourly bars start on the hour; the 9:00 one (from 9:30) is dropped
                rows, first = rows[30:], first + 30 * 60 * 10**9
            agg = _aggregate(rows, size) if size > 1 else rows
            stamps.append(first + np.arange(len(agg), dtype=np.int64) * size * 60 * 10**9)
            blocks.append(agg)
        if not blocks:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
        idx = pd.DatetimeIndex(np.concatenate(stamps), tz="UTC", name="timestamp")
        df = pd.DataFrame(np.vstack(blocks), index=idx, columns=["open", "high", "low", "close", "volume"])
        finished = df.index + pd.Timedelta(minutes=size) <= end_ts
        return df[finished & (df.index >= start_ts)]

    def last(self, symbol: str) -> float:
        """The latest price: this minute's during market hours, else the last close."""
        now = _utc(self.now())
        df = self.bars(symbol, "1Min", now - pd.Timedelta(hours=20))
        if not df.empty:
            return float(df["close"].iloc[-1])
        daily = self.bars(symbol, "1Day", now - pd.Timedelta(days=10))
        return float(daily["close"].iloc[-1])

    def close_on(self, symbol: str, day: date) -> float | None:
        d = _daily(symbol)
        ts = pd.Timestamp(day, tz=NY).tz_convert("UTC")
        return float(d.at[ts, "close"]) if ts in d.index else None


def market_clock(now: datetime) -> SimpleNamespace:
    """Weekdays 9:30-16:00 New York time (holidays aren't modelled)."""
    t = now.astimezone(NY)

    def session(d: date):
        return (datetime.combine(d, time(9, 30), NY), datetime.combine(d, time(16, 0), NY))

    d = t.date()
    while True:
        o, c = session(d)
        if d.weekday() < 5 and t < c:
            break
        d += timedelta(days=1)
    is_open = o <= t < c
    nxt_open = o if not is_open else None
    if is_open:
        d2 = d + timedelta(days=1)
        while d2.weekday() >= 5:
            d2 += timedelta(days=1)
        nxt_open = session(d2)[0]
    return SimpleNamespace(is_open=is_open, timestamp=now, next_open=nxt_open.astimezone(UTC),
                           next_close=c.astimezone(UTC))


# --- the made-up account's history ---------------------------------------------------------------

@dataclass
class Holding:
    symbol: str
    qty: float
    avg: float
    owner: str            # strategy name, or "invest"
    opened: datetime
    stop: float | None = None
    target: float | None = None


@dataclass
class Book:
    """Everything the account did: Alpaca's side (fills) and the bot's side (journal rows)."""
    fills: list[Fill] = field(default_factory=list)
    strategy_of: dict[str, str] = field(default_factory=dict)
    holdings: dict[str, Holding] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)       # journal order rows
    invest: list[dict] = field(default_factory=list)       # journal invest_orders rows
    events: list[tuple[datetime, str, str, str]] = field(default_factory=list)  # (ts, level, kind, message)
    ai_trades: list[dict] = field(default_factory=list)    # the AI calls that became orders

    @property
    def cash(self) -> float:
        return START_CASH + sum(f.qty * f.price * (1 if f.side == "sell" else -1) for f in self.fills)


def _strategy(symbol: str) -> StrategyConfig:
    return next(sc for sc in STRATEGIES if symbol in sc.symbols)


def _hour_bars(md: DemoMarketData, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly bars starting 10:00-14:00 (so an order at the bar's close lands in market hours)."""
    df = md.bars(symbol, "1Hour", start, end)
    hours = df.index.tz_convert(NY).hour
    return df[(hours >= 10) & (hours <= 14)]


def _at(ts: pd.Timestamp, seconds: int) -> datetime:
    """When an order at this hourly bar's close fills: just after the bar ends."""
    return (ts + pd.Timedelta(hours=1, seconds=seconds)).to_pydatetime()


class _Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix: str = "demo") -> str:
        self.n += 1
        return f"{prefix}-{self.n:05d}"


def _buy_reason(sc: StrategyConfig, r: np.random.Generator) -> str:
    if sc.name == "macd_rsi":
        return f"MACD crossed up below zero; RSI hit {r.uniform(30, 38):.1f} in last 18 bars"
    if sc.name == "ma_20_rsi":
        return f"RSI {r.uniform(30, 35):.1f} < 35.5"
    return "AI buy"  # replaced with the model's own words in _ai_rows


def _sell_reason(sc: StrategyConfig, r: np.random.Generator) -> str:
    if sc.name == "macd_rsi":
        return f"RSI {r.uniform(70.2, 76):.1f} > 70"
    if sc.name == "ma_20_rsi":
        return "price crossed below MA20"
    return "AI sell"


def last_session(now: datetime) -> date:
    """The latest weekday whose close (16:00 New York) has passed: the day the daily email is about."""
    t = now.astimezone(NY)
    day = t.date() if t.time() >= time(16) else t.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def build_book(md: DemoMarketData, now: datetime) -> Book:
    """A year of made-up trading: about 25 closed trades (a little over half winners), three open
    positions with stops, and a monthly invest plan. The latest session has the most going on: two
    trades closed at a gain, one at a loss, and a new buy. Every price comes from the demo bars."""
    book, ids = Book(), _Ids()
    r = _rng(9)
    n_total = sum(TRADES.values())
    wins = round(n_total * WIN_RATE)
    outcomes = list(r.permutation([True] * wins + [False] * (n_total - wins)))
    window_start = now - timedelta(days=330)
    session = last_session(now)
    end = datetime.combine(session, time(16), NY)
    bars, days = {}, {}
    for symbol, n in TRADES.items():
        sc = _strategy(symbol)
        bars[symbol] = _hour_bars(md, symbol, window_start, end)
        days[symbol] = sorted({ts.tz_convert(NY).date() for ts in bars[symbol].index})
        usable = days[symbol][: len(days[symbol]) - 12]  # the last two weeks are for the latest trades
        slots = np.linspace(0, len(usable) - 12, n).astype(int) + r.integers(0, 6, n)
        for slot in slots:
            _trade(book, ids, md, sc, symbol, bars[symbol], usable, int(slot), outcomes.pop(), r)
    for loser in LAST_SESSION_CLOSES:
        plan = {s: _session_trade(book, _strategy(s), s, bars[s], days[s], s != loser, r) for s in LAST_SESSION_CLOSES}
        if all(plan.values()):
            for s, (entry_ts, entry, exit_ts, exit_price) in plan.items():
                _round_trip(book, ids, _strategy(s), s, entry_ts, entry, exit_ts, exit_price, r)
            break
    for symbol, back in STILL_OPEN.items():
        _open_position(book, ids, md, _strategy(symbol), symbol, bars[symbol], days[symbol], back, r)
    _invest(book, ids, md, now)
    book.fills.sort(key=lambda f: f.ts)
    book.orders.sort(key=lambda o: o["ts"])
    return book


def _trade(book: Book, ids: _Ids, md, sc: StrategyConfig, symbol: str, bars: pd.DataFrame, days: list[date],
           slot: int, win: bool, r: np.random.Generator) -> None:
    """One closed round trip opened around days[slot], ending as a win or a loss as asked when the
    prices allow it (otherwise it starts a day later and tries again)."""
    for attempt in range(12):
        day = days[min(slot + attempt, len(days) - 1)]
        on_day = bars[(bars.index.tz_convert(NY).date == day) & (bars.index > _last_fill(book, symbol))]
        if on_day.empty:
            continue
        entry_ts = on_day.index[int(r.integers(0, len(on_day)))]
        entry = float(bars.at[entry_ts, "close"])
        later = bars[bars.index > entry_ts].iloc[: 6 * int(r.integers(3, 9))]
        stop = entry * (1 - sc.stop_loss_pct / 100)
        if later.empty:
            continue
        if win:
            goal = entry * (1 + r.uniform(0.015, 0.06))
            hit = later[later["close"] >= goal]
            if hit.empty:
                best = later["close"].idxmax()
                hit = later.loc[[best]] if later.at[best, "close"] > entry * 1.004 else hit
        else:
            goal = entry * (1 - r.uniform(0.012, 0.045))
            hit = later[later["close"] <= goal]
        if hit.empty:
            continue
        exit_ts = hit.index[0]
        if (later.loc[:exit_ts, "low"] <= stop).any():
            continue  # would have hit its stop first; keep the story simple
        _round_trip(book, ids, sc, symbol, entry_ts, entry, exit_ts, float(hit.at[exit_ts, "close"]), r)
        return


def _last_fill(book: Book, symbol: str) -> pd.Timestamp:
    """After this symbol's last fill: one round trip at a time per stock."""
    ts = [f.ts for f in book.fills if f.symbol == symbol]
    return _utc(max(ts)) if ts else pd.Timestamp(FIRST_DAY, tz="UTC")


def _row(**fields) -> dict:
    return fields


def _qty(sc: StrategyConfig, price: float) -> int:
    return max(1, math.floor(START_CASH * sc.allocation_pct / 100 / price))


def _round_trip(book: Book, ids: _Ids, sc, symbol, entry_ts, entry, exit_ts, exit_price, r) -> None:
    qty = _qty(sc, entry)
    _buy(book, ids, sc, symbol, qty, entry_ts, entry, r)
    oid = ids()
    when = _at(exit_ts, int(r.integers(12, 40)))
    book.fills.append(Fill(when, symbol, "sell", qty, round(exit_price, 2), oid))
    reason = _sell_reason(sc, r)
    book.orders.append(_row(ts=when - timedelta(seconds=3), strategy=sc.name, symbol=symbol, side="sell", qty=qty,
                            est_price=round(exit_price, 2), reason=reason, broker_order_id=oid, status="filled",
                            fill_price=round(exit_price, 2), fill_ts=when))
    if sc.name == "ai":
        book.ai_trades.append({"symbol": symbol, "ts": when - timedelta(seconds=5), "question": "exit",
                               "action": "sell", "order": book.orders[-1]})


def _buy(book: Book, ids: _Ids, sc, symbol, qty, entry_ts, entry, r) -> dict:
    oid = ids()
    when = _at(entry_ts, int(r.integers(12, 40)))
    price = round(entry, 2)
    book.fills.append(Fill(when, symbol, "buy", qty, price, oid))
    book.strategy_of[oid] = sc.name
    row = _row(ts=when - timedelta(seconds=3), strategy=sc.name, symbol=symbol, side="buy", qty=qty,
               est_price=price, stop_price=round(price * (1 - sc.stop_loss_pct / 100), 2),
               take_profit_price=round(price * (1 + sc.take_profit_pct / 100), 2) if sc.take_profit_pct else None,
               reason=_buy_reason(sc, r), broker_order_id=oid, status="filled", fill_price=price, fill_ts=when)
    book.orders.append(row)
    if sc.name == "ai":
        book.ai_trades.append({"symbol": symbol, "ts": when - timedelta(seconds=5), "question": "entry",
                               "action": "buy", "order": row})
    return row


def _session_trade(book: Book, sc, symbol, bars, days, win: bool, r) -> tuple | None:
    """(entry time, entry, exit time, exit) for a round trip bought in the two weeks before the latest
    session and sold during it, at a gain or a loss as asked (at least 1%, never past its stop).
    None if the prices don't allow one."""
    session = bars[bars.index.tz_convert(NY).date == days[-1]]
    worst = -(sc.stop_loss_pct - 0.5) / 100
    for day in days[-10:-1][::-1]:
        before = bars[(bars.index.tz_convert(NY).date == day) & (bars.index > _last_fill(book, symbol))]
        for entry_ts in before.index[r.permutation(len(before))]:
            entry = float(bars.at[entry_ts, "close"])
            stop = entry * (1 - sc.stop_loss_pct / 100)
            if (bars.loc[entry_ts:session.index[-1], "low"] <= stop).any():
                continue
            for exit_ts in session.index:
                pct = float(bars.at[exit_ts, "close"]) / entry - 1
                if (0.01 <= pct <= 0.07) if win else (worst <= pct <= -0.01):
                    return entry_ts, entry, exit_ts, float(bars.at[exit_ts, "close"])
    return None


def _open_position(book: Book, ids: _Ids, md: DemoMarketData, sc, symbol, bars, days, back: range, r) -> None:
    """The position held now: bought `back` trading days before the latest session, still above its stop."""
    price_now = md.last(symbol)
    candidates = [days[-1 - b] for b in back] + days[-9:-1][::-1]  # then any day of the last two weeks
    for day in candidates:
        on_day = bars[(bars.index.tz_convert(NY).date == day) & (bars.index > _last_fill(book, symbol))]
        if on_day.empty:
            continue
        ts = on_day.index[int(r.integers(0, len(on_day)))]
        entry = float(bars.at[ts, "close"])
        since = bars[bars.index > ts]
        stop = entry * (1 - sc.stop_loss_pct / 100)
        if price_now > stop * 1.02 and (since.empty or since["low"].min() > stop):
            break
    qty = _qty(sc, entry)
    row = _buy(book, ids, sc, symbol, qty, ts, entry, r)
    book.holdings[symbol] = Holding(symbol, qty, row["fill_price"], sc.name, row["fill_ts"], row["stop_price"],
                                    row["take_profit_price"])


def _invest(book: Book, ids: _Ids, md: DemoMarketData, now: datetime) -> None:
    """The invest plan's monthly $200, split so the mix drifts back toward its weights."""
    from taylortrader.invest import split

    weights = INVEST.weights
    month = date.fromisoformat(INVEST.start)
    units = {s: 0.0 for s in weights}
    while True:
        bars = None
        for d in range(7):
            day = month + timedelta(days=d)
            if day.weekday() < 5:
                start = datetime.combine(day, time(10), NY)
                bars = {s: md.bars(s, "1Hour", start, start + timedelta(hours=1)) for s in weights}
                break
        if bars is None or any(b.empty for b in bars.values()):
            break
        prices = {s: float(b["close"].iloc[0]) for s, b in bars.items()}
        when = bars["VTI"].index[0]
        values = {s: units[s] * prices[s] for s in weights}
        alloc = split(INVEST.amount, values, weights)
        period = f"{month:%Y-%m}"
        for s, notional in alloc.items():
            oid = ids("demo-inv")
            qty = round(notional / prices[s], 6)
            ts = _at(when, 20)
            book.fills.append(Fill(ts, s, "buy", qty, round(prices[s], 2), oid))
            units[s] += qty
            book.orders.append(_row(ts=ts - timedelta(seconds=5), strategy="invest", symbol=s, side="buy", qty=qty,
                                    est_price=round(prices[s], 2), reason=f"invest plan {period}: ${notional:,.2f}",
                                    broker_order_id=oid, status="filled", fill_price=round(prices[s], 2),
                                    fill_ts=ts, period=period, notional=notional))
        book.events.append((_at(when, 25), "info", "invest",
                            f"{period} paper: " + ", ".join(f"{s} ${n:,.2f}" for s, n in alloc.items())))
        month = (month.replace(day=28) + timedelta(days=5)).replace(day=1)
        if datetime.combine(month, time(11), NY) > now:
            break
    for s, q in units.items():
        buys = [f for f in book.fills if f.symbol == s and f.side == "buy"]
        cost = sum(f.qty * f.price for f in buys)
        book.holdings[s] = Holding(s, round(q, 6), cost / q, "invest", buys[0].ts)


# --- the broker --------------------------------------------------------------------------------------

class DemoBroker:
    """Stands in for Alpaca. Reads come from the made-up book; anything you ask it to do (sell, cancel)
    is only written down, never sent anywhere."""

    mode = "paper"

    def __init__(self, md: DemoMarketData, book: Book, now=lambda: datetime.now(UTC)):
        self.md, self.book, self.now = md, book, now
        self.selling: set[str] = set()
        self.requests: list[tuple[str, str]] = []  # what the dashboard asked for: (action, symbol)

    def clock(self):
        return market_clock(self.now())

    def _value(self, h: Holding) -> float:
        return h.qty * self.md.last(h.symbol)

    def positions(self) -> dict[str, Position]:
        return {s: Position(s, h.qty, round(self._value(h), 2), round(h.avg, 4)) for s, h in self.book.holdings.items()}

    def snapshot(self) -> AccountSnapshot:
        positions = self.positions()
        cash = self.book.cash
        equity = cash + sum(p.market_value for p in positions.values())
        history = self.portfolio_history("1M")
        today = self.now().astimezone(NY).date()
        before = [v for t, v in history if datetime.fromtimestamp(t, NY).date() < today]
        start = before[-1] if before else equity
        bought = sum(1 for f in self.book.fills if f.side == "buy" and f.order_id in self.book.strategy_of
                     and f.ts.astimezone(NY).date() == today)
        return AccountSnapshot(equity=round(equity, 2), cash=round(cash, 2), day_start_equity=round(start, 2),
                               positions=positions, trades_today=bought)

    def exit_levels(self) -> dict[str, dict]:
        return {s: {"stop": h.stop, "target": h.target} for s, h in self.book.holdings.items() if h.stop}

    def symbols_with_stop(self) -> set[str]:
        return {s for s, h in self.book.holdings.items() if h.stop}

    def symbols_selling(self) -> set[str]:
        return set(self.selling)

    def open_orders(self) -> list:
        return [SimpleNamespace(symbol=s, side="sell", qty=h.qty, stop_price=h.stop)
                for s, h in self.book.holdings.items() if h.stop]

    def fills(self, days: int = 365) -> list[Fill]:
        since = self.now() - timedelta(days=days)
        return [f for f in self.book.fills if f.ts >= since]

    def portfolio_history(self, period: str = "1M") -> list[tuple[int, float]]:
        """Each trading day's closing equity: cash plus what was held, at that day's close."""
        now = self.now()
        back = {"1M": 31, "3M": 92, "1A": 366}.get(period, 31)
        out = []
        fills = sorted(self.book.fills, key=lambda f: f.ts)
        for ts in _days()[(_days() >= pd.Timestamp((now - timedelta(days=back)).date()))]:
            day = ts.date()
            close_at = datetime.combine(day, time(16), NY)
            if close_at > now:
                break
            cash, held = START_CASH, {}
            for f in fills:
                if f.ts > close_at:
                    break
                sign = 1 if f.side == "buy" else -1
                held[f.symbol] = held.get(f.symbol, 0.0) + sign * f.qty
                cash -= sign * f.qty * f.price
            value = cash + sum(q * (self.md.close_on(s, day) or 0.0) for s, q in held.items() if abs(q) > 1e-9)
            out.append((int(datetime.combine(day, time(0), NY).timestamp()), round(value, 2)))
        today = now.astimezone(NY)
        if market_clock(now).is_open:  # today so far
            out.append((int(datetime.combine(today.date(), time(0), NY).timestamp()),
                        round(self.book.cash + sum(self._value(h) for h in self.book.holdings.values()), 2)))
        return out

    def market_days(self, start: date, end: date) -> set[date]:
        return {d.date() for d in pd.bdate_range(start, end)}

    def asset_status(self, symbol: str) -> tuple[bool, bool]:
        return True, True

    def account_info(self) -> dict:
        return {"account": "…0000", "status": "ACTIVE", "equity": self.snapshot().equity, "paper": True}

    def get_order(self, order_id: str):
        return SimpleNamespace(id=order_id, status="filled")

    def order_by_client_id(self, client_order_id: str):
        return None

    # Writes: written down, never sent.

    def close_position(self, symbol: str, reason: str = ""):
        self.requests.append(("sell", symbol))
        self.selling.add(symbol)
        return SimpleNamespace(id=f"demo-sell-{len(self.requests)}")

    def cancel_all_orders(self, wait: float = 0.0) -> None:
        self.requests.append(("cancel_all", ""))

    def place_stop(self, symbol: str, qty: float, stop_price: float):
        self.requests.append(("stop", symbol))

    def submit_entry(self, intent, client_order_id: str | None = None):
        self.requests.append(("buy", intent.symbol))
        return SimpleNamespace(id=f"demo-buy-{len(self.requests)}")

    def submit_notional_buy(self, symbol: str, notional: float, client_order_id: str):
        self.requests.append(("invest", symbol))
        return SimpleNamespace(id=client_order_id)


# --- the journal --------------------------------------------------------------------------------------

def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _say(symbol: str, question: str, action: str, f: dict, r: np.random.Generator) -> tuple[str, list, list]:
    """A one-line reason with pros and cons, in the model's usual style, from the bar's real indicators."""
    rsi, above_long, above_short = f.get("rsi", 50), f["close"] > f.get("ma_200", 0), f["close"] > f.get("ma_20", 0)
    up = f.get("macd_diff", 0) > 0
    trend = "above its 200-bar average" if above_long else "below its 200-bar average"
    pros, cons = [], []
    (pros if above_long else cons).append(f"Price is {trend}")
    (pros if up else cons).append("MACD above its signal: momentum turning up" if up else "MACD below its signal")
    (pros if rsi < 45 else cons).append(f"RSI {rsi:.0f}" + (", pulled back" if rsi < 45 else ", not much of a dip"))
    (pros if f.get("chaikin", 0) > 0 else cons).append(
        "Money flowing in (Chaikin above 0)" if f.get("chaikin", 0) > 0 else "Money flowing out (Chaikin below 0)")
    if question == "entry" and action == "buy":
        dip = f"pulled back to RSI {rsi:.0f}" if rsi < 45 else f"is holding up (RSI {rsi:.0f})"
        trend_part = f"and stays {trend}" if above_long else f"though it's still {trend}"
        momentum = "MACD is crossing up" if up else "selling looks to be drying up"
        text = r.choice([
            f"{symbol} {dip} {trend_part}; {momentum} and there's no bad news, so the dip looks temporary.",
            f"Buying {symbol}: it {dip} {trend_part}, {momentum}, and recent news is neutral to positive.",
        ])
    elif question == "entry":
        if not above_long:
            text = (f"{symbol} is {trend} with momentum still {'weak' if not up else 'only just turning'}, so the "
                    "dip isn't clearly temporary; hold.")
        elif not up:
            text = f"{symbol} is {trend} but MACD is still below its signal; wait for momentum to turn up first."
        elif rsi > 55:
            text = f"{symbol} has already bounced (RSI {rsi:.0f}); buying now would be chasing the move, so hold."
        else:
            text = r.choice([
                f"Mixed picture for {symbol}: RSI {rsi:.0f} is neutral and there's no clear catalyst, so hold.",
                (f"Holding off on {symbol}: the setup is close, but sector news is soft and momentum is only "
                "neutral."),
            ])
    elif action == "sell":
        text = r.choice([
            (f"Momentum on {symbol} has faded: MACD crossed below its signal and price slipped under the 20-bar "
            "average, so the reason for buying is gone."),
            f"Selling {symbol}: RSI {rsi:.0f} after the run-up and momentum rolling over; take the gain.",
        ])
    else:
        gap = (f["close"] / f["ma_20"] - 1) * 100 if f.get("ma_20") else 0.0
        text = r.choice([
            (f"{symbol} is still {gap:.1f}% above its 20-bar average with momentum intact; nothing has changed the "
            "reason for holding."),
            f"Keep holding {symbol}: the uptrend is intact (RSI {rsi:.0f}) and there's no negative news.",
            f"{symbol} is consolidating after the move up; momentum hasn't faded enough to sell.",
        ] if above_short else [
            (f"{symbol} dipped {abs(gap):.1f}% under its 20-bar average, but the long trend holds and the stop is well "
            "below; keep holding for now."),
            f"A pullback in {symbol} (RSI {rsi:.0f}) looks like normal noise, not a broken trend; hold.",
            (f"{symbol} is soft today with the sector, but the reason for buying still stands; hold and let the stop "
            "do its job."),
        ])
    return str(text), pros[:3], cons[:3]


def _ai_rows(md: DemoMarketData, book: Book, now: datetime) -> list[dict]:
    """Every question put to the AI strategy: the calls behind its trades, and the holds in between."""
    from taylortrader.indicators import add_all
    from taylortrader.strategies.ai import features

    sc = next(s for s in STRATEGIES if s.name == "ai")
    r = _rng(11)
    rows = []
    for symbol in sc.symbols:
        bars = add_all(md.bars(symbol, "1Hour", now - timedelta(days=420), now))
        mine = sorted([t for t in book.ai_trades if t["symbol"] == symbol], key=lambda t: t["ts"])

        def feats(ts: datetime, bars=bars) -> dict:
            done = bars[bars.index + pd.Timedelta(hours=1) <= _utc(ts)]
            return features(done.iloc[-1].to_dict())

        def holding(ts: datetime, mine=mine) -> bool:
            last = [t for t in mine if t["ts"] <= ts]
            return bool(last) and last[-1]["action"] == "buy"

        for t in mine:
            f = feats(t["ts"])
            text, pros, cons = _say(symbol, t["question"], t["action"], f, r)
            conf = round(float(r.uniform(0.66, 0.82)), 2)
            rows.append(_row(ts=t["ts"], symbol=symbol, question=t["question"], action=t["action"], confidence=conf,
                             summary=text, pros=pros, cons=cons, acted=1, features=f))
            t["order"]["reason"] = f"AI {t['action']} ({conf:.0%} sure): {text}"[:300]
        # The last few weeks of questions that didn't lead anywhere.
        recent = bars[(bars.index >= _utc(now - timedelta(days=24))) & (bars.index.tz_convert(NY).hour <= 14)]
        closes = bars["close"]
        for ts in recent.index:
            when = _at(ts, int(r.integers(2, 9)))
            if when > now or any(abs((t["ts"] - when).total_seconds()) < 7200 for t in mine):
                continue
            q = "exit" if holding(when) else "entry"
            f = feats(when)
            if q == "entry" and not (f.get("rsi", 50) < 55):
                continue  # the model is mostly asked about dips
            # A model that's a bit better than a coin flip: asked more often where holding turns out right.
            k = closes.index.get_loc(ts)
            ahead = closes.iloc[k + 6] / closes.iloc[k] - 1 if k + 6 < len(closes) else 0.0
            right = ahead < 0 if q == "entry" else ahead > 0
            if r.random() > (0.5 if right else 0.22):
                continue
            action = "buy" if q == "entry" and r.random() < 0.12 else "hold"
            conf = round(float(r.uniform(0.55, 0.64) if action == "buy" else r.uniform(0.58, 0.8)), 2)
            text, pros, cons = _say(symbol, q, action, f, r)
            rows.append(_row(ts=when, symbol=symbol, question=q, action=action, confidence=conf, summary=text,
                             pros=pros, cons=cons, acted=0, features=f))
    return sorted(rows, key=lambda x: x["ts"])


LEGACY = ["VRT", "NVDA", "AMD", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "TSLA", "AVGO"]


def _session(now: datetime, days_back: int, hour: int, minute: int) -> datetime:
    """A moment during market hours `days_back` weekdays before today (New York time)."""
    day = now.astimezone(NY).date()
    while days_back > 0 or day.weekday() >= 5:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days_back -= 1
    return datetime.combine(day, time(hour, minute), NY)


def seed_journal(journal: Journal, md: DemoMarketData, book: Book, now: datetime) -> None:
    db = journal.db
    r = _rng(12)
    ai = _ai_rows(md, book, now)  # first: it writes the AI's reasons into its orders
    # Two buys the guardrails stopped, so the orders list shows what that looks like.
    for days_back, symbol, why in ((2, "PLTR", "total exposure would be 93% of equity (limit 90%)"),
                                   (7, "AAPL", "max_trades_per_day reached (10)")):
        ts = _session(now, days_back, int(r.integers(10, 15)), int(r.integers(0, 59)))
        price = round(float(md.bars(symbol, "1Hour", ts - timedelta(days=4), ts)["close"].iloc[-1]), 2)
        sc = _strategy(symbol)
        reason = ""
        if sc.name == "ai":
            from taylortrader.indicators import add_all
            from taylortrader.strategies.ai import features

            hist = add_all(md.bars(symbol, "1Hour", ts - timedelta(days=60), ts))
            reason = _say(symbol, "entry", "buy", features(hist.iloc[-1].to_dict()), r)[0]
        book.orders.append(_row(ts=ts, strategy=sc.name, symbol=symbol, side="buy", qty=_qty(sc, price),
                                est_price=price, stop_price=round(price * (1 - sc.stop_loss_pct / 100), 2),
                                reason=f"AI buy (68% sure): {reason}"[:300] if reason else _buy_reason(sc, r),
                                broker_order_id=None, status="blocked", detail=why))
    for o in sorted(book.orders, key=lambda o: o["ts"]):
        db.execute(
            "INSERT INTO orders (ts, mode, strategy, symbol, side, qty, est_price, stop_price, take_profit_price, "
            "reason, broker_order_id, status, fill_price, fill_ts, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(o["ts"]), "paper", o["strategy"], o["symbol"], o["side"], o["qty"], o["est_price"],
             o.get("stop_price"), o.get("take_profit_price"), o.get("reason", ""), o["broker_order_id"], o["status"],
             o.get("fill_price"), _iso(o["fill_ts"]) if o.get("fill_ts") else None, o.get("detail", "")))
        if o["strategy"] == "invest":
            db.execute("INSERT INTO invest_orders (ts, period, mode, symbol, notional, planned_total, status, attempts,"
                       " order_id) VALUES (?,?,?,?,?,?, 'submitted', 1, last_insert_rowid())",
                       (_iso(o["ts"]), o["period"], "paper", o["symbol"], round(o["notional"], 2), INVEST.amount))
    for row in ai:
        db.execute(
            "INSERT INTO ai_decisions (ts, strategy, symbol, question, action, confidence, summary, pros, cons, acted,"
            " model, input_tokens, output_tokens, latency_ms, features) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(row["ts"]), "ai", row["symbol"], row["question"], row["action"], row["confidence"], row["summary"],
             json.dumps(row["pros"]), json.dumps(row["cons"]), row["acted"], AIConfig().model,
             int(r.integers(2100, 3400)), int(r.integers(140, 260)), int(r.integers(3500, 9500)),
             json.dumps(row["features"])))
    # Events: a quiet, healthy week.
    events = list(book.events)
    for d in range(1, 8):
        day = _session(now, d, 16, 5)
        events.append((day, "info", "summary", str(day.date())))
    plan = ", ".join(f"{sc.name} {sc.symbols} {sc.timeframe}" for sc in STRATEGIES)
    vrt = book.holdings["VRT"]
    events += [
        (_session(now, 6, 8, 41), "info", "start", f"PAPER: {plan}; invest plan {INVEST.symbols}"),
        (_session(now, 4, 13, 12), "warning", "no-prices", "No new prices for PLTR"),
        (_session(now, 4, 13, 31), "info", "feed", "PLTR prices are back"),
        (_session(now, 3, 19, 2), "info", "settings", "settings saved from the dashboard"),
        (_session(now, 3, 19, 3), "info", "restart", "settings changed: restarting with the new ones"),
        (_session(now, 3, 19, 3) + timedelta(seconds=4), "info", "start", f"PAPER: {plan}; invest plan {INVEST.symbols}"),
        (vrt.opened + timedelta(seconds=3), "info", "protect",
         f"VRT stop-loss confirmed at Alpaca: ${vrt.stop:,.2f}"),
    ]
    for ts, level, kind, message in sorted(events):
        if ts <= now:
            db.execute("INSERT INTO events (ts, level, kind, message) VALUES (?,?,?,?)", (_iso(ts), level, kind, message))
    # Trades imported from an older bot, for the "Imported trades" panel.
    for i in range(46):
        year = 2022 + i // 16
        buy = date(year, 1, 3) + timedelta(days=int(r.integers(0, 340)))
        sell = buy + timedelta(days=int(r.integers(2, 25)))
        price = round(float(r.uniform(40, 400)), 2)
        pct = float(r.normal(0.8, 6.0))
        shares = max(1, int(1500 // price))
        db.execute("INSERT INTO legacy_trades (buy_date, sell_date, symbol, strategy, buy_price, sell_price, shares,"
                   " percent_change, profit, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (buy.isoformat(), sell.isoformat(), str(r.choice(LEGACY)), str(r.choice(["macd", "rsi_14"])),
                    price, round(price * (1 + pct / 100), 2), shares, round(pct, 2),
                    round(shares * price * pct / 100, 2), "demo-import.db"))
    journal.beat()


def config(data_dir: Path) -> Config:
    return Config(mode="paper", dry_run=False, data_dir=data_dir, feed="yahoo", strategies=list(STRATEGIES),
                  limits=Limits(), invest=INVEST,
                  alerts=AlertsConfig(email_from="you@example.com", email_to=["you@example.com"]))


def make_runtime(folder: Path | None = None, now=lambda: datetime.now(UTC)):
    """A Runtime on the made-up account, with its settings and journal in `folder` (a new temp folder
    by default)."""
    from taylortrader.web.runtime import Runtime

    folder = Path(folder or tempfile.mkdtemp(prefix="taylortrader-demo-"))
    cfg_path = folder / "config.toml"
    save_config(config(folder / "var"), cfg_path)
    md = DemoMarketData(now)
    book = build_book(md, now())
    broker = DemoBroker(md, book, now)
    rt = Runtime(config_path=cfg_path, dotenv_path=None, connect=lambda cfg, dotenv: (broker, md, list(ALERT_NOTES)),
                 demo=True)
    seed_journal(rt.journal, md, book, now())
    return rt


def summary(rt, now: datetime | None = None):
    """The daily summary email for the demo account's latest session, built the way the service builds
    it (Service.daily_summary) from the same broker, journal and settings the dashboard shows."""
    from taylortrader.alerts import Alerts
    from taylortrader.service import Service

    now = now or rt.broker.now()
    close = datetime.combine(last_session(now), time(16, 5), NY).astimezone(UTC)
    md = DemoMarketData(lambda: close)
    broker = DemoBroker(md, rt.broker.book, lambda: close)
    svc = Service(rt.settings, broker, md, rt.journal, Alerts(), now=lambda: close, sleep=lambda s: None)
    return svc.daily_summary(broker.snapshot())


def keep_beating(journal: Journal, every: float = 20.0) -> threading.Event:
    """A heartbeat like the real service's, so the demo shows "Running". Set the returned event to stop."""
    stop = threading.Event()

    def loop():
        while not stop.wait(every):
            journal.beat()

    threading.Thread(target=loop, daemon=True, name="demo-heartbeat").start()
    return stop
