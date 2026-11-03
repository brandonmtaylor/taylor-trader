import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from guardrails import AccountSnapshot, Limits, Position
from taylortrader.alerts import Alerts, Level, Notifier
from taylortrader.config import Config, ConfigError, ServiceConfig, StrategyConfig, load_config
from taylortrader.journal import Journal
from taylortrader.service import Service, completed_bars, kill_file, watchdog
from taylortrader.strategies import Signal, Strategy, register

# --- test doubles -------------------------------------------------------------------------------

FLAGS = {"entry": False, "exit": False}


@register
class SignalStub(Strategy):
    name = "test_signal"
    description = "test double driven by FLAGS"

    @dataclass(frozen=True)
    class Params:
        pass

    @property
    def warmup(self):
        return 5

    def entry(self, b, i):
        return Signal(FLAGS["entry"], "test entry")

    def exit(self, b, i, entry_price):
        return Signal(FLAGS["exit"], "test exit")


class Recorder(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, title, message, level=Level.INFO):
        self.sent.append((title, message, level))

    def titles(self):
        return [t for t, _, _ in self.sent]


class Boom(Notifier):
    def send(self, *a, **k):
        raise RuntimeError("channel down")


NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)  # Wed 10:00 New York


class FakeBroker:
    mode = "paper"

    def __init__(self, is_open=True):
        self.is_open = is_open
        self.positions = {}
        self.stops = set()
        self.orders = []
        self.entries, self.closes, self.stops_placed = [], [], []
        self.order_status = {}
        self.fail_clock = False

    def clock(self):
        if self.fail_clock:
            raise ConnectionError("alpaca down")
        return SimpleNamespace(is_open=self.is_open, timestamp=NOW, next_open=NOW + timedelta(hours=20),
                               next_close=NOW + timedelta(hours=6))

    def snapshot(self):
        return AccountSnapshot(equity=10_000, cash=10_000 - sum(p.market_value for p in self.positions.values()),
                               day_start_equity=10_000, positions=dict(self.positions))

    def symbols_with_stop(self):
        return set(self.stops)

    def open_orders(self):
        return self.orders

    def submit_entry(self, intent):
        self.entries.append(intent)
        return SimpleNamespace(id=f"o{len(self.entries)}")

    def close_position(self, symbol, reason=""):
        self.closes.append(symbol)
        return SimpleNamespace(id=f"c{len(self.closes)}")

    def place_stop(self, symbol, qty, stop):
        self.stops_placed.append((symbol, qty, round(stop, 2)))
        self.stops.add(symbol)

    def get_order(self, oid):
        return self.order_status[oid]


class FakeData:
    def bars(self, symbol, timeframe, start, end=None):
        idx = pd.date_range(NOW - timedelta(minutes=5 * 50), periods=50, freq="5min", tz="UTC")
        c = np.linspace(100, 110, 50)
        return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0}, index=idx)


@pytest.fixture
def svc(tmp_path):
    FLAGS.update(entry=False, exit=False)
    cfg = Config(data_dir=tmp_path, strategies=[
        StrategyConfig(name="test_signal", symbols=["VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)],
        limits=Limits(), service=ServiceConfig())
    rec = Recorder()
    s = Service(cfg, FakeBroker(), FakeData(), Journal(tmp_path / "j.db"), Alerts(push=[rec], email=[rec]),
                now=lambda: NOW, sleep=lambda s: None)
    s.rec = rec
    return s


# --- service --------------------------------------------------------------------------------------

def test_entry_goes_to_broker_with_stop_and_is_journaled(svc):
    FLAGS["entry"] = True
    svc.tick()
    intent = svc.broker.entries[0]
    assert intent.symbol == "VRT" and intent.qty == 18  # 20% of $10k at $110
    assert intent.stop_loss_price == pytest.approx(99.0)
    row = svc.journal.recent_orders()[0]
    assert row["status"] == "submitted" and row["broker_order_id"] == "o1"
    assert any("Buying VRT" in t for t in svc.rec.titles())


def test_same_bar_is_not_traded_twice(svc):
    FLAGS["entry"] = True
    svc.tick()
    svc.broker.entries.clear()
    svc.tick()
    assert svc.broker.entries == []


def test_exit_signal_closes_position(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    svc.broker.stops.add("VRT")
    FLAGS["exit"] = True
    svc.tick()
    assert svc.broker.closes == ["VRT"]


def test_kill_switch_blocks_entries_not_exits(svc):
    kill_file(svc.cfg).write_text("on")
    FLAGS["entry"] = True
    svc.tick()
    assert svc.broker.entries == []
    assert svc.journal.recent_orders()[0]["status"] == "blocked"
    assert any("Kill switch ON" in t for t in svc.rec.titles())
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    svc.broker.stops.add("VRT")
    svc.last_bar.clear()
    FLAGS.update(entry=False, exit=True)
    svc.tick()
    assert svc.broker.closes == ["VRT"]


def test_dry_run_never_touches_broker(svc):
    svc.dry_run = svc.executor.dry_run = True
    FLAGS["entry"] = True
    svc.tick()
    assert svc.broker.entries == [] and svc.journal.recent_orders()[0]["status"] == "dry-run"


def test_missing_stop_on_owned_position_is_recreated(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)  # $110/share, entry $100
    svc.tick()
    assert svc.broker.stops_placed == [("VRT", 18, 90.0)]


def test_unprotected_position_below_stop_exits_with_critical_alert(svc):
    # The VRT scenario: bought at 315, no stop, now 224.
    svc.broker.positions["VRT"] = Position("VRT", 36, 36 * 224, 315)
    svc.tick()
    assert svc.broker.closes == ["VRT"]
    assert any(level is Level.CRITICAL for _, _, level in svc.rec.sent)


def test_unmanaged_position_alerts_but_is_not_touched(svc):
    svc.broker.positions["AAPL"] = Position("AAPL", 1, 330, 268)
    svc.tick()
    svc.tick()
    assert svc.broker.closes == [] and svc.broker.stops_placed == []
    assert sum("Unmanaged position: AAPL" in t for t in svc.rec.titles()) == 1  # once a day


def test_broker_side_stop_hit_is_reported(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    svc.broker.stops.add("VRT")
    svc.tick()
    del svc.broker.positions["VRT"]
    svc.tick()
    assert any("Position closed: VRT" in t for t in svc.rec.titles())


def test_fills_are_recorded(svc):
    FLAGS["entry"] = True
    svc.tick()
    svc.broker.order_status["o1"] = SimpleNamespace(status="filled", filled_avg_price="110.05",
                                                     filled_at="2026-11-04T15:05:00Z", filled_qty="18")
    FLAGS["entry"] = False
    svc.tick()
    row = svc.journal.recent_orders()[0]
    assert row["status"] == "filled" and row["fill_price"] == pytest.approx(110.05)
    assert any("Filled: BUY VRT" in t for t in svc.rec.titles())


def test_closed_market_sends_one_daily_summary(svc, tmp_path):
    svc.broker.is_open = False
    svc.now = lambda: datetime(2026, 11, 4, 21, 10, tzinfo=UTC)  # 16:10 New York
    svc.tick()
    svc.summary_date = None  # simulate a restart: must not resend
    svc.tick()
    assert sum(t.startswith("Daily summary") for t in svc.rec.titles()) == 2  # email + push, once
    assert svc.journal.last_beat() is not None


def test_errors_alert_then_stop_after_five(svc):
    svc.broker.fail_clock = True
    with pytest.raises(SystemExit):
        svc.run_forever()
    titles = svc.rec.titles()
    assert "TaylorTrader error" in titles and "TaylorTrader stopping" in titles
    assert svc.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='error'").fetchone()[0] == 5


def test_completed_bars_drops_forming_bar():
    idx = pd.date_range("2026-11-04 14:30", periods=3, freq="5min", tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    assert len(completed_bars(df, "5Min", datetime(2026, 11, 4, 14, 42, tzinfo=UTC))) == 2
    assert len(completed_bars(df, "5Min", datetime(2026, 11, 4, 14, 45, tzinfo=UTC))) == 3


# --- watchdog -------------------------------------------------------------------------------------

def test_watchdog_alerts_when_stale_and_when_recovered(tmp_path):
    j, rec = Journal(tmp_path / "j.db"), Recorder()
    state = tmp_path / "wd"
    assert not watchdog(j, Alerts(push=[rec]), 15, state, now=NOW)  # never beat
    assert not watchdog(j, Alerts(push=[rec]), 15, state, now=NOW + timedelta(minutes=10))  # no repeat < 1h
    assert rec.titles() == ["TaylorTrader is NOT running"]
    j.beat()
    assert watchdog(j, Alerts(push=[rec]), 15, state)
    assert rec.titles()[-1] == "TaylorTrader is back"


# --- alerts, journal, config ---------------------------------------------------------------------

def test_alert_channel_failure_never_raises_and_repeats_are_suppressed():
    rec = Recorder()
    a = Alerts(push=[Boom(), rec])
    assert a.send("x", "y")
    assert not a.send("x", "y")
    assert rec.titles() == ["x"]


def test_legacy_import(tmp_path):
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.execute("CREATE TABLE transactions (buy_date, stock, buy_price, hold_time, sell_date, sell_price, "
               "percent_increase, total_num_shares_bought, profit, strategy)")
    db.execute("INSERT INTO transactions VALUES ('2026-06-26','VRT',311.04,'','2026-06-30',311.82,0.25,36,27.99,'MACD_RSI')")
    db.execute("INSERT INTO transactions VALUES ('2026-07-01','VRT',315.07,'','','','',36,'','MACD_RSI')")
    db.commit()
    j = Journal(tmp_path / "j.db")
    assert j.import_legacy(old) == 2
    assert j.import_legacy(old) == 2  # idempotent
    assert tuple(j.db.execute("SELECT COUNT(*), COUNT(sell_date) FROM legacy_trades").fetchone()) == (2, 1)


def test_one_strategy_owns_each_symbol(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[strategies]]\nname="rsi_14"\nsymbols=["VRT"]\n[[strategies]]\nname="macd"\nsymbols=["VRT"]\n')
    with pytest.raises(ConfigError, match="One strategy owns each symbol"):
        load_config(cfg)


def test_config_secret_names(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[alerts]\nntfy_server = "https://ntfy.example.com"\n')
    assert load_config(cfg).alerts.ntfy_server == "https://ntfy.example.com"
    cfg.write_text('[alerts]\nntfy_topic = "abc"\n')
    with pytest.raises(ConfigError, match="looks like a secret"):
        load_config(cfg)


def test_one_symbol_failing_does_not_stop_the_others(svc):
    class FlakyData(FakeData):
        def bars(self, symbol, timeframe, start, end=None):
            if symbol == "BAD":
                raise ConnectionError("no data for BAD")
            return super().bars(symbol, timeframe, start, end)

    FLAGS["entry"] = True
    svc.market_data = FlakyData()
    svc.cfg = dataclasses.replace(svc.cfg, strategies=[
        StrategyConfig(name="test_signal", symbols=["BAD", "VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)])
    svc.tick()  # does not raise
    assert [i.symbol for i in svc.broker.entries] == ["VRT"]
    errors = svc.journal.db.execute("SELECT message FROM events WHERE kind='error'").fetchall()
    assert len(errors) == 1 and "BAD" in errors[0][0]


def test_every_symbol_failing_raises_into_the_error_count(svc):
    class DownData(FakeData):
        def bars(self, *a, **k):
            raise ConnectionError("network down")

    svc.market_data = DownData()
    with pytest.raises(ConnectionError):
        svc.tick()
