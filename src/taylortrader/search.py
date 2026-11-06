"""An honest strategy search: try a small fixed list, test the winner once on data it never saw.

Trying many strategies and keeping the best is how backtests fool people: with 30 tries, a few
will look great by luck. So:

1. The data is split by time. The search only sees the older part (default 70%).
2. Every candidate on a fixed list is backtested there, against buy-and-hold. The list is small and
   doesn't grow when nothing passes, and every result is reported, not just the winner.
3. The winner is tested once on the newest part (the holdout). That's the number to believe; its
   search-period number is optimistic because it was picked as the best of many.
4. Luck test: on the holdout, random entries with the winner's trade frequency, holding time and
   stop are run many times. The winner should beat most of them, or its edge could be luck.
5. Each look at a holdout is logged. Rerunning the search after looking makes the holdout less clean,
   and the report says so.

Nothing here places orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from taylortrader import backtest
from taylortrader.strategies import Bars, Signal, Strategy, get_strategy
from taylortrader.strategies.base import NO

# Fixed lists chosen by a rule, not by how they did afterward (picking past winners makes holding
# them look unbeatable). Each is what it was on the date in its name.
UNIVERSES = {
    # The Dow Jones Industrial Average on 2020-01-01. UTX became RTX in April 2020 (Raytheon merger).
    "dow2020": ("AAPL", "AXP", "BA", "CAT", "CSCO", "CVX", "DIS", "DOW", "GS", "HD", "IBM", "INTC", "JNJ", "JPM",
                "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "PFE", "PG", "RTX", "TRV", "UNH", "V", "VZ", "WBA",
                "WMT", "XOM"),
    # The S&P 500 sector funds that existed in 2018 (communications, XLC, started that June).
    "sectors": ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"),
}

BUILTINS = ("macd_rsi", "macd", "ma_20", "rsi_14", "ma_20_rsi", "ma_200_rsi", "ma_20_over_200_rsi", "chaikin",
            "pullback")
RULES = (
    ("rsi < 50 and macd_diff > 0", "rsi > 68 or macd_diff < 0"),        # dip with momentum turning up
    ("rsi < 35", "rsi > 65"),                                            # plain oversold / overbought
    ("prev_rsi < 30 and rsi >= 30", "rsi > 70"),                         # bounce out of oversold
    ("close > ma_200 and rsi < 40", "rsi > 65"),                         # dip in a long uptrend
    ("close > ma_200 and macd_diff > 0 and prev_macd_diff <= 0", "macd_diff < 0"),  # momentum turn in uptrend
)
STOPS = (8.0, 12.0)
CASH_YIELD = 4.0      # % a year on uninvested cash (roughly T-bills / money market in 2024-26)
MIN_TRADES = 10       # across all symbols in the search period; fewer is too few to judge
PASS_PERCENTILE = 90  # the winner must beat this share of random runs on the holdout


@dataclass(frozen=True)
class Candidate:
    strategy: str
    params: dict = field(default_factory=dict)
    stop_pct: float = 12.0

    @property
    def label(self) -> str:
        if self.strategy == "rule":
            return f"rule: buy {self.params['buy_when']} / sell {self.params['sell_when']} · stop {self.stop_pct:g}%"
        return f"{self.strategy} · stop {self.stop_pct:g}%"


def default_grid(stops=STOPS) -> list[Candidate]:
    grid = [Candidate(name, {}, s) for name in BUILTINS for s in stops]
    grid += [Candidate("rule", {"buy_when": b, "sell_when": x}, s) for b, x in RULES for s in stops]
    return grid


# --- running one candidate ---------------------------------------------------------------------------

def split_at(df: pd.DataFrame, holdout: float) -> int:
    return int(len(df) * (1 - holdout))


def _run(df: pd.DataFrame, strat: Strategy, stop: float, start: int, end: int, slippage_bps: float,
         cash_yield: float = CASH_YIELD):
    """Backtest bars [start, end): indicators warm up on the bars just before `start`, but trading
    starts at `start`, so nothing outside the window is traded."""
    lo = max(0, start - strat.warmup)
    part = df.iloc[lo:end]
    if len(part) <= strat.warmup + 2:
        return None
    return backtest.run(part, strat, stop_loss_pct=stop, slippage_bps=slippage_bps, cash_yield_pct=cash_yield)


def evaluate(c: Candidate, frames: dict[str, pd.DataFrame], window: str, holdout: float,
             slippage_bps: float = 5.0) -> dict:
    """One candidate over every symbol, in the search period ("search") or the holdout ("holdout")."""
    per = {}
    holds = []
    for sym, df in frames.items():
        s = split_at(df, holdout)
        start, end = (get_strategy(c.strategy, c.params).warmup, s) if window == "search" else (s, len(df))
        r = _run(df, get_strategy(c.strategy, c.params), c.stop_pct, start, end, slippage_bps)
        if r is None:
            continue
        per[sym] = {"return": r.total_return_pct, "buy_hold": r.buy_hold_return_pct,
                    "excess": r.total_return_pct - r.buy_hold_return_pct, "trades": len(r.trades),
                    "max_dd": r.max_drawdown_pct, "buy_hold_max_dd": r.buy_hold_max_drawdown_pct,
                    "exposure": r.exposure_pct}
        holds += [_bars_held(df, t) for t in r.trades]
    n = len(per)
    out = {"label": c.label, "candidate": asdict(c), "symbols": per,
           "trades": sum(p["trades"] for p in per.values()),
           "excess": statistics.mean(p["excess"] for p in per.values()) if n else None,
           "return": statistics.mean(p["return"] for p in per.values()) if n else None,
           "buy_hold": statistics.mean(p["buy_hold"] for p in per.values()) if n else None,
           "max_dd": statistics.mean(p["max_dd"] for p in per.values()) if n else None,
           "buy_hold_max_dd": statistics.mean(p["buy_hold_max_dd"] for p in per.values()) if n else None,
           "exposure": statistics.mean(p["exposure"] for p in per.values()) if n else None,
           "median_bars_held": int(statistics.median(holds)) if holds else None}
    total_bars = sum(max(0, (split_at(df, holdout) if window == "search" else len(df) - split_at(df, holdout)))
                     for df in frames.values())
    out["trades_per_bar"] = out["trades"] / total_bars if total_bars else 0.0
    return out


def _bars_held(df: pd.DataFrame, t) -> int:
    return max(1, int(((df.index >= t.entry_time) & (df.index <= t.exit_time)).sum()))


# --- the luck test -------------------------------------------------------------------------------------

class RandomEntry(Strategy):
    """Buys at random (a set chance per bar) and sells after a set number of bars: no skill at all."""
    name = "random"
    description = "random entries, for the luck test"
    backtestable = True

    @dataclass(frozen=True)
    class Params:
        chance: float = 0.01
        hold_bars: int = 7
        seed: int = 0
        warm: int = 35

    def __init__(self, params=None):
        super().__init__(params)
        self._rng = np.random.default_rng(self.p.seed)
        self._held = 0

    @property
    def warmup(self) -> int:
        return self.p.warm

    def entry(self, b: Bars, i: int) -> Signal:
        if self._rng.random() < self.p.chance:
            self._held = 0
            return Signal(True, "random")
        return NO

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        self._held += 1
        return Signal(True, "random") if self._held >= self.p.hold_bars else NO


def luck_test(winner: dict, frames: dict[str, pd.DataFrame], holdout: float, seeds: int,
              slippage_bps: float = 5.0) -> list[float]:
    """Average excess over buy-and-hold on the holdout, for `seeds` random-entry runs shaped like the winner."""
    c = winner["candidate"]
    warm = get_strategy(c["strategy"], c["params"]).warmup
    chance = max(winner["trades_per_bar"], 1e-4)
    hold = max(1, winner["median_bars_held"] or 7)
    results = []
    for seed in range(seeds):
        ex = []
        for k, (_sym, df) in enumerate(frames.items()):
            strat = RandomEntry({"chance": chance, "hold_bars": hold, "seed": seed * 1000 + k, "warm": warm})
            r = _run(df, strat, c["stop_pct"], split_at(df, holdout), len(df), slippage_bps)
            if r is not None:
                ex.append(r.total_return_pct - r.buy_hold_return_pct)
        if ex:
            results.append(statistics.mean(ex))
    return results


# --- the whole search -------------------------------------------------------------------------------------

def run_search(frames: dict[str, pd.DataFrame], grid: list[Candidate], holdout: float = 0.3, seeds: int = 100,
               slippage_bps: float = 5.0, log_path: Path | None = None, on_progress=None) -> dict:
    # Same window for every symbol (one listed later than the others would otherwise get its own,
    # later, split date), so "the holdout" is one calendar period for all of them.
    common = max(df.index[0] for df in frames.values())
    frames = {s: df[df.index >= common] for s, df in frames.items()}
    rows = []
    for k, c in enumerate(grid):
        rows.append(evaluate(c, frames, "search", holdout, slippage_bps))
        if on_progress:
            on_progress(k + 1, len(grid), c.label)
    eligible = [r for r in rows if r["excess"] is not None and r["trades"] >= MIN_TRADES]
    rows.sort(key=lambda r: (r["excess"] is None, -(r["excess"] or -math.inf)))
    eligible.sort(key=lambda r: -r["excess"])

    first = next(iter(frames.values()))
    s = split_at(first, holdout)
    periods = {"search": [str(first.index[0].date()), str(first.index[s - 1].date())],
               "holdout": [str(first.index[s].date()), str(first.index[-1].date())]}
    report = {"symbols": list(frames), "tried": len(grid), "eligible": len(eligible), "periods": periods,
              "holdout_share": holdout, "min_trades": MIN_TRADES, "results": rows, "winner": None,
              "holdout": None, "luck": None, "verdict": None, "previous_looks": 0}
    if not eligible:
        report["verdict"] = (f"Nothing made {MIN_TRADES} trades in the search period, so there's no winner to test.")
        return report

    winner = eligible[0]
    report["winner"] = winner
    look_key = _look_key(frames, periods)
    report["previous_looks"] = _count_looks(log_path, look_key)
    held = evaluate(Candidate(**winner["candidate"]), frames, "holdout", holdout, slippage_bps)
    report["holdout"] = held
    _log_look(log_path, look_key, winner["label"], held["excess"])

    luck = luck_test(winner, frames, holdout, seeds, slippage_bps)
    beat = (sum(x < held["excess"] for x in luck) / len(luck) * 100) if luck and held["excess"] is not None else None
    report["luck"] = {"runs": len(luck), "median": statistics.median(luck) if luck else None,
                      "p90": float(np.percentile(luck, 90)) if luck else None, "beat_pct": beat}
    report["verdict"] = verdict(held, beat)
    return report


def verdict(held: dict, beat: float | None) -> str:
    ex = held["excess"]
    if ex is None or held["trades"] == 0:
        return "INCONCLUSIVE: the winner didn't trade in the holdout."
    if ex > 0 and beat is not None and beat >= PASS_PERCENTILE:
        return (f"PASSED: beat buy-and-hold on data it never saw ({ex:+.2f} pts) and beat {beat:.0f}% of random "
                "entries. Worth running on paper, not proof.")
    if ex > 0:
        return (f"NOT PROVEN: beat buy-and-hold on the holdout ({ex:+.2f} pts), but only {beat:.0f}% of random "
                f"entries did worse (needs {PASS_PERCENTILE}%). Could be luck.")
    if beat is not None and beat >= PASS_PERCENTILE:
        return (f"FAILED, with a caveat: lost to buy-and-hold on data it never saw ({ex:+.2f} pts), but its timing "
                f"beat {beat:.0f}% of random entries. The timing has something; being out of the market part of "
                "the time cost more than it earned.")
    return (f"FAILED: lost to buy-and-hold on data it never saw ({ex:+.2f} pts), and its timing wasn't better "
            "than random entries. Its search-period result was luck.")


# --- holdout look log ------------------------------------------------------------------------------------

def _look_key(frames, periods) -> str:
    raw = json.dumps({"symbols": sorted(frames), "holdout": periods["holdout"]})
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _count_looks(path: Path | None, key: str) -> int:
    if not path or not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip() and json.loads(line)["key"] == key)


def _log_look(path: Path | None, key: str, label: str, excess) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"key": key, "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                            "winner": label, "holdout_excess": excess}) + "\n")
