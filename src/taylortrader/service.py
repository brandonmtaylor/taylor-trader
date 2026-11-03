"""The always-on trading service.

One loop, run by launchd (which restarts it if it dies):
  every tick: heartbeat -> fills -> protection check -> (market open) evaluate strategies on new bars
  after the close: one daily summary (the "I'm alive" message you should expect every trading day)

The original bot died silently for four months. Here, every failure path ends in an alert:
loop errors are pushed (rate-limited); 5 in a row stops the process with a critical alert so
launchd restarts it; and a separate watchdog process alerts if the heartbeat goes stale.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from guardrails import Guardrails, find_unmanaged_positions
from taylortrader import indicators
from taylortrader.alerts import Alerts, Level
from taylortrader.config import Config, StrategyConfig
from taylortrader.executor import Executor
from taylortrader.journal import Journal
from taylortrader.strategies import Bars, Strategy, get_strategy

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

BAR = {"1Min": timedelta(minutes=1), "5Min": timedelta(minutes=5), "15Min": timedelta(minutes=15),
       "1Hour": timedelta(hours=1), "1Day": timedelta(days=1)}
LOOKBACK = {"1Min": timedelta(days=3), "5Min": timedelta(days=10), "15Min": timedelta(days=20),
            "1Hour": timedelta(days=90), "1Day": timedelta(days=420)}
MAX_CONSECUTIVE_ERRORS = 5


def kill_file(cfg: Config) -> Path:
    return cfg.data_dir / "KILL"


def completed_bars(df: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
    """Drop the bar that is still forming. Daily bars count as complete only after the close."""
    if df.empty:
        return df
    last = df.index[-1]
    if timeframe == "1Day":
        done = now.astimezone(NY).date() > last.astimezone(NY).date() or now.astimezone(NY).hour >= 16
        return df if done else df.iloc[:-1]
    return df if last + BAR[timeframe] <= now else df.iloc[:-1]


@dataclass
class Service:
    cfg: Config
    broker: object
    market_data: object
    journal: Journal
    alerts: Alerts
    dry_run: bool = False
    healthcheck_url: str | None = None
    now: callable = field(default=lambda: datetime.now(UTC))
    sleep: callable = field(default=time.sleep)

    def __post_init__(self):
        self.guardrails = Guardrails(self.cfg.limits)
        self.executor = Executor(self.broker, self.guardrails, self.journal, self.alerts, self.dry_run)
        self.strategies: dict[str, Strategy] = {sc.name: get_strategy(sc.name, sc.params)
                                                for sc in self.cfg.enabled_strategies}
        self.owner: dict[str, StrategyConfig] = {s: sc for sc in self.cfg.enabled_strategies for s in sc.symbols}
        self.last_bar: dict[tuple[str, str], pd.Timestamp] = {}
        self.known_positions: dict[str, float] = {}
        self.kill_on = False
        self.errors = 0
        self.summary_date = None
        self.open_note_date = None
        self.last_ping = 0.0
        self.unmanaged_note_date: dict[str, object] = {}

    # --- lifecycle -----------------------------------------------------------------------------

    def run_forever(self) -> None:
        mode = "DRY RUN" if self.dry_run else self.broker.mode.upper()
        plan = ", ".join(f"{sc.name} {sc.symbols} {sc.timeframe}" for sc in self.cfg.enabled_strategies) or "none"
        self.journal.event("start", f"{mode}: {plan}")
        self.alerts.send(f"TaylorTrader started ({mode})", f"Strategies: {plan}", key="start")
        try:
            while True:
                try:
                    wait = self.tick()
                    self.errors = 0
                except Exception as e:
                    self.errors += 1
                    log.exception("loop error %d/%d", self.errors, MAX_CONSECUTIVE_ERRORS)
                    self.journal.event("error", f"{type(e).__name__}: {e}", "error")
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        self.alerts.send("TaylorTrader stopping", f"{self.errors} errors in a row. Last: {e}\n"
                                         "launchd will restart it; check var/logs.", Level.CRITICAL, key="fatal")
                        raise SystemExit(1) from e
                    self.alerts.send("TaylorTrader error", f"{type(e).__name__}: {e}", Level.WARNING,
                                     key=f"err:{type(e).__name__}")
                    wait = min(60 * self.errors, 300)
                self.sleep(wait)
        except KeyboardInterrupt:
            self.journal.event("stop", "stopped by user")
            self.alerts.send("TaylorTrader stopped", "Stopped by user (Ctrl-C or launchctl).", key="stop")

    def tick(self) -> float:
        self.journal.beat("dry-run" if self.dry_run else "ok")
        self._ping_healthcheck()
        self._check_kill_switch()
        clock = self.broker.clock()
        self.check_fills()
        snap = self.broker.snapshot()
        self.check_positions(snap)

        if not clock.is_open:
            self._maybe_daily_summary(snap)
            until_open = (clock.next_open - clock.timestamp).total_seconds()
            return max(30.0, min(300.0, until_open))

        self._maybe_open_note()
        # One symbol failing (bad data, a rejected order) must not stop the others from being
        # checked. If every symbol fails, it's systemic: raise so the loop's error count applies.
        jobs = [(sc, symbol) for sc in self.cfg.enabled_strategies for symbol in sc.symbols]
        failures = []
        for sc, symbol in jobs:
            try:
                self.evaluate(sc, symbol, snap)
            except Exception as e:  # isolated per symbol, reported below
                log.exception("%s %s failed", sc.name, symbol)
                failures.append((sc, symbol, e))
        if failures and len(failures) == len(jobs):
            raise failures[0][2]
        for sc, symbol, e in failures:
            msg = f"{sc.name} {symbol}: {type(e).__name__}: {e}"
            self.journal.event("error", msg, "error")
            self.alerts.send("TaylorTrader error", f"{msg}\nOther symbols kept running.", Level.WARNING,
                             key=f"err:{sc.name}:{symbol}:{type(e).__name__}")
        return float(self.cfg.service.loop_seconds)

    # --- pieces --------------------------------------------------------------------------------

    def _check_kill_switch(self) -> None:
        on = kill_file(self.cfg).exists()
        if on != self.kill_on:
            self.kill_on = on
            self.guardrails.kill_switch = on
            state = "ON: no new positions; exits and stops still work" if on else "OFF: trading resumed"
            self.journal.event("kill_switch", state, "warning" if on else "info")
            self.alerts.send(f"Kill switch {state.split(':')[0]}", state, Level.WARNING, key=f"kill:{on}")

    def _ping_healthcheck(self) -> None:
        if not self.healthcheck_url or time.monotonic() - self.last_ping < 300:
            return
        self.last_ping = time.monotonic()
        try:
            urllib.request.urlopen(self.healthcheck_url, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            log.warning("healthcheck ping failed: %s", e)

    def _maybe_open_note(self) -> None:
        today = self.now().astimezone(NY).date()
        if self.open_note_date != today:
            self.open_note_date = today
            plan = ", ".join(f"{sc.name} {sc.symbols}" for sc in self.cfg.enabled_strategies) or "no strategies"
            self.alerts.send("Market open", f"Running: {plan}" + (" (KILL SWITCH ON)" if self.kill_on else ""),
                             key=f"open:{today}")

    def check_fills(self) -> None:
        for row in self.journal.pending_orders():
            try:
                order = self.broker.get_order(row["broker_order_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't look up order %s: %s", row["broker_order_id"], e)
                continue
            status = str(getattr(order.status, "value", order.status))
            if status == "filled":
                px = float(order.filled_avg_price)
                self.journal.mark_filled(row["id"], px, str(order.filled_at))
                stop = f", stop ${row['stop_price']:,.2f}" if row["stop_price"] else ""
                self.alerts.send(f"Filled: {row['side'].upper()} {row['symbol']}",
                                 f"{float(order.filled_qty):g} @ ${px:,.2f}{stop}\n{row['reason']}")
            elif status in ("canceled", "expired", "rejected"):
                self.journal.mark_status(row["id"], status)
                self.alerts.send(f"Order {status}: {row['side']} {row['symbol']}", row["reason"] or "",
                                 Level.WARNING)

    def check_positions(self, snap) -> None:
        held = {s: p.qty for s, p in snap.positions.items()}

        # A position we knew about vanished and we didn't sell it: the broker-side stop fired.
        for symbol in set(self.known_positions) - set(held):
            if not self.journal.db.execute(
                "SELECT 1 FROM orders WHERE symbol=? AND side='sell' AND ts >= ?",
                (symbol, (self.now() - timedelta(hours=2)).isoformat()),
            ).fetchone():
                self.journal.event("stop_hit", f"{symbol} position closed at the broker (stop or take-profit)")
                self.alerts.send(f"Position closed: {symbol}", "Closed at Alpaca by its stop-loss or take-profit "
                                 "order.", Level.WARNING)
        self.known_positions = held

        stops = self.broker.symbols_with_stop()
        managed = set(self.owner)
        today = self.now().astimezone(NY).date()
        for symbol, issues in find_unmanaged_positions(snap.positions, stops, managed).items():
            pos = snap.positions[symbol]
            sc = self.owner.get(symbol)
            if sc and self.cfg.service.auto_protect and "no stop-loss order at the broker" in issues:
                self._protect(sc, pos)
            elif sc is None and self.unmanaged_note_date.get(symbol) != today:
                self.unmanaged_note_date[symbol] = today
                self.alerts.send(f"Unmanaged position: {symbol}", "; ".join(issues) +
                                 ". TaylorTrader won't touch it; add it to a strategy or manage it yourself.",
                                 Level.WARNING, key=f"unmanaged:{symbol}:{today}")

    def _protect(self, sc: StrategyConfig, pos) -> None:
        """A position a strategy owns has no stop. Recreate it, or exit if the stop level is already breached."""
        stop = pos.avg_entry_price * (1 - sc.stop_loss_pct / 100)
        price = pos.market_value / pos.qty if pos.qty else 0
        if self.dry_run:
            self.alerts.send(f"[dry run] {pos.symbol} has no stop", f"Would place stop at ${stop:,.2f}",
                             Level.WARNING, key=f"protect:{pos.symbol}")
            return
        if price and price <= stop:
            self.journal.event("protect", f"{pos.symbol} at ${price:,.2f} is below its stop ${stop:,.2f}; exiting",
                               "warning")
            self.alerts.send(f"{pos.symbol}: stop level breached while unprotected",
                             f"Price ${price:,.2f} is below the {sc.stop_loss_pct}% stop (${stop:,.2f}). Exiting.",
                             Level.CRITICAL, key=f"protect:{pos.symbol}")
            self.executor.exit(sc.name, pos.symbol, pos.qty, price, "stop level breached while unprotected")
            return
        try:
            self.broker.place_stop(pos.symbol, pos.qty, stop)
            self.journal.event("protect", f"recreated stop for {pos.symbol} at ${stop:,.2f}", "warning")
            self.alerts.send(f"Recreated stop: {pos.symbol}", f"Stop was missing. New GTC stop at ${stop:,.2f}.",
                             Level.WARNING, key=f"protect:{pos.symbol}")
        except Exception as e:  # noqa: BLE001
            self.alerts.send(f"{pos.symbol} has NO stop", f"Couldn't recreate it: {e}", Level.CRITICAL,
                             key=f"protect:{pos.symbol}")

    def evaluate(self, sc: StrategyConfig, symbol: str, snap) -> None:
        now = self.now()
        df = self.market_data.bars(symbol, sc.timeframe, now - LOOKBACK[sc.timeframe])
        df = completed_bars(df, sc.timeframe, now)
        strat = self.strategies[sc.name]
        if len(df) <= strat.warmup:
            log.warning("%s %s: only %d bars, need > %d", sc.name, symbol, len(df), strat.warmup)
            return
        key = (sc.name, symbol)
        if self.last_bar.get(key) == df.index[-1]:
            return  # no new bar since last decision
        self.last_bar[key] = df.index[-1]

        b = Bars(indicators.add_all(df))
        i = len(df) - 1
        price = float(b.close[i])
        pos = snap.positions.get(symbol)
        if pos and pos.qty > 0:
            sig = strat.exit(b, i, pos.avg_entry_price)
            if sig:
                self.executor.exit(sc.name, symbol, pos.qty, price, f"{sc.name}: {sig.reason}")
        else:
            if any(o.symbol == symbol and o.side.value == "buy" for o in self.broker.open_orders()):
                return  # a buy is already working
            sig = strat.entry(b, i)
            if sig:
                self.executor.enter(sc, symbol, price, sig.reason)

    def _maybe_daily_summary(self, snap) -> None:
        if not self.cfg.service.daily_summary:
            return
        now_ny = self.now().astimezone(NY)
        today = now_ny.date()
        if self.summary_date == today or now_ny.weekday() >= 5 or (now_ny.hour, now_ny.minute) < (16, 5):
            return
        self.summary_date = today
        if self.journal.db.execute("SELECT 1 FROM events WHERE kind='summary' AND message=?",
                                   (str(today),)).fetchone():
            return  # already sent today (e.g. the service restarted after the close)
        self.alerts.summary(f"Daily summary {today}", self.daily_summary_text(snap))
        self.journal.event("summary", str(today))

    def daily_summary_text(self, snap) -> str:
        start = datetime.combine(self.now().astimezone(NY).date(), datetime.min.time(), NY).astimezone(UTC)
        orders = self.journal.orders_since(start.isoformat())
        errors = [e for e in self.journal.events_since(start.isoformat()) if e["level"] in ("error", "warning")]
        stops = self.broker.symbols_with_stop()
        mode = "DRY RUN" if self.dry_run else self.broker.mode.upper()
        lines = [
            f"[{mode}] Equity ${snap.equity:,.2f} ({snap.day_pnl_pct:+.2f}% today), cash ${snap.cash:,.2f}",
            f"Kill switch: {'ON' if self.kill_on else 'off'}. Orders today: {len(orders)}. Problems today: {len(errors)}.",
            "",
            "Positions:" if snap.positions else "Positions: none",
        ]
        for p in snap.positions.values():
            owner = self.owner.get(p.symbol)
            lines.append(f"  {p.symbol}: {p.qty:g} sh, ${p.market_value:,.2f}, avg ${p.avg_entry_price:,.2f}, "
                         f"stop {'yes' if p.symbol in stops else 'NO'}, "
                         f"{'managed by ' + owner.name if owner else 'unmanaged'}")
        if orders:
            lines += ["", "Orders:"] + [
                f"  {o['ts'][11:16]} {o['side']} {o['symbol']} {o['qty'] or ''} [{o['status']}] {o['reason']}"
                for o in orders]
        if errors:
            lines += ["", "Problems:"] + [f"  {e['ts'][11:16]} {e['kind']}: {e['message']}" for e in errors[:20]]
        lines += ["", "If you stop getting this summary on trading days, TaylorTrader isn't running."]
        return "\n".join(lines)


# --- watchdog (separate process) ---------------------------------------------------------------

def watchdog(journal: Journal, alerts: Alerts, stale_minutes: int, state_file: Path,
             now: datetime | None = None) -> bool:
    """Alert if the service's heartbeat is stale. Returns True if healthy.

    Runs as its own short-lived launchd job, so it still works when the service has crashed.
    Re-alerts at most once an hour while the problem lasts (tracked in state_file).
    """
    now = now or datetime.now(UTC)
    beat = journal.last_beat()
    age = (now - beat[0]) if beat else None
    if age is not None and age <= timedelta(minutes=stale_minutes):
        if state_file.exists():
            state_file.unlink()
            alerts.send("TaylorTrader is back", f"Heartbeat {int(age.total_seconds())}s ago.", key="watchdog-ok")
        return True
    last_alert = datetime.fromisoformat(state_file.read_text()) if state_file.exists() else None
    if last_alert is None or now - last_alert >= timedelta(hours=1):
        ago = f"{int(age.total_seconds() // 60)} minutes" if age is not None else "never"
        alerts.send("TaylorTrader is NOT running", f"Last heartbeat: {ago} ago.\n"
                    "Check: launchctl print gui/$UID/com.taylortrader.service, and var/logs.",
                    Level.CRITICAL, key="watchdog")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(now.isoformat())
    return False
