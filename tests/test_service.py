import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from taylortrader.alerts import Alerts, Level, Notifier
from taylortrader.config import Config, ConfigError, ServiceConfig, StrategyConfig, load_config, parse_config
from taylortrader.guardrails import AccountSnapshot, Limits, Position
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
        self.selling = set()
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

    def exit_levels(self):
        levels = getattr(self, "levels", {})  # tests set exact prices here; otherwise any stop will do
        return {s: levels.get(s, {"stop": 1.0, "target": None}) for s in self.stops} | {
            s: v for s, v in levels.items() if s not in self.stops}

    def symbols_selling(self):
        return set(self.selling)

    def open_orders(self):
        return self.orders

    def submit_entry(self, intent, client_order_id=None):
        # Like Alpaca: a repeated client order id is refused.
        seen = self.__dict__.setdefault("client_ids", [])
        if client_order_id and client_order_id in seen:
            raise RuntimeError('{"code":40010001,"message":"client_order_id must be unique"}')
        if client_order_id:
            seen.append(client_order_id)
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
    cfg = Config(data_dir=tmp_path, dry_run=False, strategies=[
        StrategyConfig(name="test_signal", symbols=["VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)],
        limits=Limits(), service=ServiceConfig())
    rec = Recorder()
    s = Service(cfg, FakeBroker(), FakeData(), Journal(tmp_path / "j.db"), Alerts(push=[rec], email=[rec]),
                now=lambda: NOW, sleep=lambda s: None)
    s.rec = rec
    return s


def _own(s, symbol):
    """The bot bought this position (so it manages it), as the journal would show."""
    s.journal.record_order(mode="paper", strategy="test_signal", symbol=symbol, side="buy", status="filled")


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
    _own(svc, "VRT")
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
    _own(svc, "VRT")
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
    _own(svc, "VRT")
    svc.tick()
    assert svc.broker.stops_placed == [("VRT", 18, 90.0)]


def test_position_with_a_sell_waiting_is_left_alone(svc):
    # Sold from the dashboard after hours: its stop was cancelled on purpose; re-adding one would block the sell.
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    _own(svc, "VRT")
    svc.broker.selling = {"VRT"}
    svc.tick()
    assert svc.broker.stops_placed == [] and svc.broker.closes == []


def test_unprotected_position_below_stop_exits_with_critical_alert(svc):
    # The VRT scenario: bought at 315, no stop, now 224.
    svc.broker.positions["VRT"] = Position("VRT", 36, 36 * 224, 315)
    _own(svc, "VRT")
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
    _own(svc, "VRT")
    svc.broker.stops.add("VRT")
    svc.tick()
    del svc.broker.positions["VRT"]
    svc.tick()
    assert any("Position closed: VRT" in t for t in svc.rec.titles())


def test_stop_hit_alert_says_the_price_and_what_it_lost(svc):
    from taylortrader.stats import Fill

    svc.broker.positions["VRT"] = Position("VRT", 4, 808, 206.44)

    _own(svc, "VRT")
    svc.broker.stops.add("VRT")
    svc.tick()
    del svc.broker.positions["VRT"]
    svc.broker.fills = lambda days=365: [Fill(NOW, "VRT", "sell", 4.0, 193.72, "s1")]
    svc.tick()
    msg = next(m for t, m, _ in svc.rec.sent if t == "Position closed: VRT")
    assert "Sold 4 @ $193.72 by its stop-loss" in msg
    assert "−$50.88 (−6.2%) vs. the $206.44 buy" in msg


def test_sell_fill_alert_says_what_it_made(svc):
    svc.broker.positions["VRT"] = Position("VRT", 10, 1100, 100)
    _own(svc, "VRT")
    svc.broker.stops.add("VRT")
    svc.tick()
    svc.journal.record_order(mode="paper", strategy="test_signal", symbol="VRT", side="sell", status="submitted",
                             qty=10, broker_order_id="c9")
    svc.broker.order_status["c9"] = SimpleNamespace(status="filled", filled_avg_price="112.5",
                                                     filled_at="2026-11-04T15:05:00Z", filled_qty="10")
    svc.check_fills()
    msg = next(m for t, m, _ in svc.rec.sent if t == "Filled: SELL VRT")
    assert "+$125.00 (+12.5%) vs. the $100.00 buy" in msg


def test_after_hours_sale_filling_at_the_open_is_not_a_stop_hit(svc):
    # Sold from the dashboard at night; the market order only fills at the open, hours later.
    svc.broker.positions["AAPL"] = Position("AAPL", 1, 328, 300)
    svc.tick()
    svc.journal.db.execute(
        "INSERT INTO orders (ts, mode, strategy, symbol, side, qty, reason, status, fill_price, fill_ts)"
        " VALUES (?, 'paper', 'dashboard', 'AAPL', 'sell', 1, 'sold from the dashboard', 'filled', 345.37, ?)",
        ((NOW - timedelta(hours=10)).isoformat(), str(NOW - timedelta(minutes=1)).replace("T", " ")))
    del svc.broker.positions["AAPL"]
    svc.tick()
    assert not any("Position closed: AAPL" in t for t in svc.rec.titles())


def test_alpaca_clients_get_a_request_timeout():
    from taylortrader.broker import HTTP_TIMEOUT, with_timeout

    seen = {}
    session = SimpleNamespace(request=lambda method, url, **kw: seen.update(kw))
    with_timeout(SimpleNamespace(_session=session))
    session.request("GET", "https://example.test")
    assert seen["timeout"] == HTTP_TIMEOUT
    session.request("GET", "https://example.test", timeout=3)
    assert seen["timeout"] == 3


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


def test_one_network_blip_is_quiet_two_in_a_row_alert(svc):
    import requests

    from taylortrader.service import is_network_error

    assert is_network_error(requests.exceptions.ReadTimeout("read timed out"))
    assert is_network_error(requests.exceptions.ConnectionError("down"))
    assert not is_network_error(ValueError("bad data"))
    calls = {"n": 0}
    real_tick = svc.tick

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("read timed out")  # one blip, then fine
        if calls["n"] in (3, 4):
            raise requests.exceptions.ReadTimeout("read timed out")  # two in a row
        if calls["n"] == 5:
            raise KeyboardInterrupt
        return real_tick()

    svc.tick = flaky
    svc.run_forever()
    errs = [m for t, m, _ in svc.rec.sent if t == "TaylorTrader error"]
    assert len(errs) == 1 and "(2 in a row)" in errs[0]


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


def test_settings_change_restarts_bad_file_is_ignored(svc, tmp_path):
    import os

    from taylortrader.config import save_config
    from taylortrader.service import RestartRequested

    path = tmp_path / "config.toml"
    cfg = dataclasses.replace(svc.cfg, strategies=[
        StrategyConfig(name="macd_rsi", symbols=["VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)])
    save_config(cfg, path)
    svc.cfg, svc.config_path = cfg, path
    svc.config_mtime = svc._mtime()
    svc.check_config()  # unchanged: nothing happens

    path.write_text(path.read_text().replace("allocation_pct = 20", "allocation_pct = 99"))  # over the limit
    os.utime(path, (1, 1))
    svc.check_config()  # invalid: reported, keeps running
    assert any("still using the old settings" in r["message"]
               for r in svc.journal.db.execute("SELECT message FROM events WHERE kind='error'"))

    save_config(dataclasses.replace(cfg, dry_run=True), path)
    os.utime(path, (2, 2))
    with pytest.raises(RestartRequested, match="paper -> dry run"):
        svc.check_config()


def test_config_writer_round_trips_awkward_values(tmp_path):
    from taylortrader.config import AlertsConfig, load_config, save_config

    cfg = Config(data_dir=tmp_path, alerts=AlertsConfig(email_from='a"b\\c@x.com', email_to=["x@y.z"]),
                 strategies=[StrategyConfig(name="macd_rsi", symbols=["BRK.B"], params={"rsi_buy": 30.5})])
    save_config(cfg, tmp_path / "c.toml")
    assert load_config(tmp_path / "c.toml") == cfg


def test_settings_change_restarts_through_the_real_loop(svc, tmp_path):
    """The restart signal must leave run_forever as a restart, not be swallowed as an error."""
    import os

    from taylortrader.config import save_config
    from taylortrader.service import RestartRequested

    path = tmp_path / "config.toml"
    cfg = svc.cfg
    save_config(cfg, path)
    svc.config_path, svc.config_mtime = path, path.stat().st_mtime
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) > 3:  # without the fix the loop would swallow the restart and spin forever
            pytest.fail("settings change didn't restart the service")
        if len(ticks) == 1:  # change the settings between the first and second tick
            save_config(dataclasses.replace(cfg, dry_run=True), path)
            os.utime(path, (5, 5))
    svc.sleep = sleep
    with pytest.raises(RestartRequested):
        svc.run_forever()
    kinds = [r[0] for r in svc.journal.db.execute("SELECT kind FROM events")]
    assert "restart" in kinds and "error" not in kinds
    assert "TaylorTrader error" not in svc.rec.titles()


def test_ai_buy_goes_through_guardrails_like_any_strategy(tmp_path):
    from taylortrader import llm
    from taylortrader.strategies.ai import AIContext

    class Model:
        def ask(self, system, user, allowed):
            return llm.validate({"action": "buy", "confidence": 0.9, "summary": "Looks good.",
                                 "pros": [], "cons": []}, allowed)

    cfg = Config(data_dir=tmp_path, dry_run=False, strategies=[
        StrategyConfig(name="ai", symbols=["VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)],
        limits=Limits(), service=ServiceConfig())
    j = Journal(tmp_path / "j.db")
    rec = Recorder()
    s = Service(cfg, FakeBroker(), FakeData(), j, Alerts(push=[rec], email=[rec]),
                ai=AIContext(journal=j, llm=Model()), now=lambda: NOW, sleep=lambda s: None)
    s.tick()
    intent = s.broker.entries[0]
    assert intent.symbol == "VRT" and intent.stop_loss_price is not None  # stop attached as always
    assert "AI buy (90% sure)" in j.recent_orders()[0]["reason"]


def test_two_blocks_of_one_strategy_keep_their_own_settings(svc):
    cfg = dataclasses.replace(svc.cfg, strategies=[
        StrategyConfig(name="rsi_14", symbols=["AAA"], params={"rsi_buy": 20}),
        StrategyConfig(name="rsi_14", symbols=["BBB"], params={"rsi_buy": 45})])
    s = Service(cfg, FakeBroker(), FakeData(), svc.journal, svc.alerts, now=lambda: NOW)
    assert s.strategy_for["AAA"].p.rsi_buy == 20 and s.strategy_for["BBB"].p.rsi_buy == 45


def test_daily_summary_html_is_escaped_and_readable(svc):
    from taylortrader import report

    svc.broker.positions["BTCUSD"] = Position("BTCUSD", 0.00004175, 3.25, 0)
    svc.journal.record_order(mode="paper", strategy="macd_rsi", symbol="VRT", side="buy", status="blocked", qty=0,
                             reason="<script>alert(1)</script>", detail="20% of $1,141 is $228")
    svc.journal.db.execute("UPDATE orders SET ts=?", (NOW.isoformat(),))  # "today" on the test clock
    s = svc.daily_summary(svc.broker.snapshot())
    text, page = report.text(s), report.html(s)
    assert "4.175e-05" not in text and "0.00004175" in text
    assert "20% of $1,141 is $228" in text and "<script>" not in page and "&lt;script&gt;" in page
    assert "BTCUSD" in page and "not the bot" in page


def test_start_ping_is_quiet_and_skipped_right_after_a_settings_restart(svc):
    def stop(_):
        raise KeyboardInterrupt
    svc.sleep = stop
    sent = []
    svc.alerts.send = lambda title, message="", level=Level.INFO, **k: sent.append((title, level))

    svc.run_forever()
    assert sent[0] == ("TaylorTrader started (PAPER)", Level.LOW)  # routine start: listed, silent

    sent.clear()
    svc.journal.event("restart", "strategies or limits updated")  # the old process said "settings changed"
    svc.run_forever()
    assert not any(t.startswith("TaylorTrader started") for t, _ in sent)


# --- review fixes (2026-11-06) ---------------------------------------------------------------------

def _tick(s):
    try:
        s.tick()
    except Exception:  # noqa: BLE001, S110 - the loop counts it; the test checks what happens next
        pass


def test_a_failed_exit_is_retried_on_the_same_bar(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    _own(svc, "VRT")
    svc.broker.stops.add("VRT")
    FLAGS["exit"] = True
    real, calls = svc.broker.close_position, []

    def flaky(symbol, reason=""):
        calls.append(symbol)
        if len(calls) == 1:
            raise RuntimeError("insufficient qty available for order (held_for_orders)")
        return real(symbol, reason)

    svc.broker.close_position = flaky
    _tick(svc)
    _tick(svc)  # right after: not retried yet, since each try lifts the stop for a moment
    assert calls == ["VRT"]
    svc.now = lambda: NOW + timedelta(minutes=6)
    _tick(svc)
    assert calls == ["VRT", "VRT"] and svc.broker.closes == ["VRT"]


def test_no_entry_on_a_stale_bar(svc):
    FLAGS["entry"] = True
    svc.now = lambda: NOW + timedelta(hours=2)  # the newest bar is 2 hours old
    svc.tick()
    assert svc.broker.entries == []


def test_a_restart_doesnt_rebuy_on_the_same_bar(svc):
    FLAGS["entry"] = True
    svc.tick()
    svc.last_bar.clear()  # what a restart (any settings save) does
    svc.tick()
    assert len(svc.broker.entries) == 1
    assert not any("FAILED" in t for t in svc.rec.titles())


def test_an_unclosable_breached_position_doesnt_stop_the_check(svc):
    svc.broker.positions["VRT"] = Position("VRT", 36, 36 * 224, 315)
    _own(svc, "VRT")

    def refuse(symbol, reason=""):
        raise RuntimeError("market closed")

    svc.broker.close_position = refuse
    svc.tick()  # doesn't raise
    assert any(level is Level.CRITICAL for _, _, level in svc.rec.sent)


def test_the_daily_summary_is_retried_after_a_blip(svc):
    svc.broker.is_open = False
    svc.now = lambda: datetime(2026, 11, 4, 21, 10, tzinfo=UTC)
    real = svc.daily_summary

    def once_down(snap):
        svc.daily_summary = real
        raise ConnectionError("blip")

    svc.daily_summary = once_down
    _tick(svc)
    _tick(svc)
    assert sum(t.startswith("Daily summary") for t in svc.rec.titles()) == 2


def test_each_fill_gets_its_own_alert(svc):
    for i in (1, 2):
        svc.journal.record_order(mode="paper", strategy="test_signal", symbol="VRT", side="sell", qty=1,
                                 status="submitted", reason="exit", broker_order_id=f"s{i}")
        svc.broker.order_status[f"s{i}"] = SimpleNamespace(
            status="filled", filled_avg_price="100", filled_at="2026-11-04T15:00:01Z", filled_qty="1")
    svc.check_fills()
    assert sum(t == "Filled: SELL VRT" for t in svc.rec.titles()) == 2


@pytest.mark.parametrize(("patch", "msg"), [
    ({"guardrails": {"max_daily_loss_pct": float("nan")}}, "must be a number"),
    ({"guardrails": {"max_trades_per_day": float("inf")}}, "must be a number"),
    ({"strategies": [{"name": "macd_rsi", "symbols": "SPY"}]}, "must be a list"),
    ({"strategies": [{"name": "macd_rsi", "symbols": ["SPY"], "take_profit_pct": float("inf")}]}, "must be a number"),
    ({"service": {"loop_seconds": 0}}, "loop_seconds"),
    ({"invest": {"enabled": True, "amount": 100, "start": date(2026, 12, 1),
                 "holdings": [{"symbol": "VTI", "weight": 100}]}}, "wrong type"),
])
def test_hand_edited_values_that_would_break_trading_are_refused(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(patch)


def test_buys_in_one_check_count_against_each_other(svc):
    # 20% each, but the exposure limit only has room for one: the snapshot can't show the first buy yet.
    svc.guardrails.limits = dataclasses.replace(svc.guardrails.limits, max_total_exposure_pct=30.0)
    sc = svc.cfg.strategies[0]
    svc.executor.enter(sc, "AAA", 100.0, "first")
    svc.executor.enter(sc, "BBB", 100.0, "second")
    assert [e.symbol for e in svc.broker.entries] == ["AAA"]
    svc.executor.inflight.clear()  # each check starts from what the account really shows


def test_a_paper_order_left_open_isnt_polled_on_the_live_account(svc):
    svc.journal.record_order(mode="live", strategy="s", symbol="VRT", side="sell", status="submitted",
                             broker_order_id="x")
    assert svc.journal.pending_orders("paper") == []
    assert not svc.journal.sold_since("VRT", "2099-01-01", "paper")


# --- quiet checks ---------------------------------------------------------------------------------

def _no_prices(s):
    return [m for t, m, _ in s.rec.sent if t == "No new prices"]


def test_frozen_prices_alert_once_a_day_and_new_bars_keep_it_quiet(svc):
    svc.tick()
    svc.now = lambda: NOW + timedelta(minutes=59)
    svc.tick()
    assert _no_prices(svc) == []
    svc.now = lambda: NOW + timedelta(minutes=61)  # FakeData's newest bar never changes
    svc.tick()
    svc.now = lambda: NOW + timedelta(minutes=90)
    svc.tick()
    assert len(_no_prices(svc)) == 2  # push + email, once
    assert "VRT" in _no_prices(svc)[0] and "Stop-losses" in _no_prices(svc)[0]
    assert next(e for e in svc.journal.events_since("") if e["kind"] == "no-prices")["level"] == "warning"


def test_too_few_bars_counts_as_no_prices(svc):
    svc.market_data.bars = lambda *a, **k: FakeData().bars("VRT", "5Min", None).iloc[:3]
    svc.tick()
    svc.now = lambda: NOW + timedelta(minutes=61)
    svc.tick()
    assert _no_prices(svc)


def test_slow_timeframes_get_three_bars_before_it_counts(svc):
    sc = svc.cfg.strategies[0]
    svc.cfg.strategies[0] = dataclasses.replace(sc, timeframe="1Hour")
    svc.market_data.bars = lambda *a, **k: FakeData().bars("VRT", "5Min", None).iloc[:3]
    svc.tick()
    svc.now = lambda: NOW + timedelta(hours=2)
    svc.tick()
    assert _no_prices(svc) == []
    svc.now = lambda: NOW + timedelta(hours=3, minutes=1)
    svc.tick()
    assert _no_prices(svc)


def test_daily_strategy_is_fine_all_session_on_its_one_new_bar(svc):
    sc = svc.cfg.strategies[0]
    svc.cfg.strategies[0] = dataclasses.replace(sc, timeframe="1Day")
    days = pd.date_range("2026-01-01", periods=50, freq="B", tz="UTC")
    svc.market_data.bars = lambda *a, **k: FakeData().bars("VRT", "5Min", None).set_axis(days)
    svc.tick()
    svc.now = lambda: NOW + timedelta(hours=5)
    svc.tick()
    assert _no_prices(svc) == []
    tomorrow = NOW + timedelta(days=1)  # same newest bar the next day: that one is frozen
    svc.now = lambda: tomorrow
    svc.tick()
    svc.now = lambda: tomorrow + timedelta(minutes=61)
    svc.tick()
    assert _no_prices(svc)


CLOSE = datetime(2026, 11, 4, 21, 10, tzinfo=UTC)  # Wed 16:10 New York


def _after_close(s, days_quiet):
    """The market is closed and the strategy's clock began days_quiet days ago."""
    s.broker.is_open = False
    s.now = lambda: CLOSE
    s.journal.event("watching", "test_signal")
    s.journal.db.execute("UPDATE events SET ts=? WHERE kind='watching'", ((CLOSE - timedelta(days=days_quiet)).isoformat(),))


def _order(s, status, days_ago):
    s.journal.record_order(mode="paper", strategy="test_signal", symbol="VRT", side="buy", status=status)
    s.journal.db.execute("UPDATE orders SET ts=? WHERE id=(SELECT MAX(id) FROM orders)",
                         ((CLOSE - timedelta(days=days_ago)).isoformat(),))


def _no_trades(s):
    return [m for t, m, _ in s.rec.sent if t == "No trades: test_signal"]


def test_no_trades_note_after_the_set_days_and_only_once(svc):
    _after_close(svc, 13)
    svc.tick()
    assert _no_trades(svc) == []
    svc.no_trades_date = None
    _after_close(svc, 14)
    svc.tick()
    assert len(_no_trades(svc)) == 2 and "hasn't traded yet" in _no_trades(svc)[0]
    svc.no_trades_date = None  # a restart, or the next day: the note starts a new stretch
    svc.alerts._last.clear()
    svc.journal.db.execute("UPDATE events SET ts=? WHERE kind='no-trades'", (CLOSE.isoformat(),))
    svc.tick()
    assert len(_no_trades(svc)) == 2


def test_a_recent_trade_resets_it_and_old_ones_are_named(svc):
    _after_close(svc, 30)
    _order(svc, "submitted", 3)
    svc.tick()
    assert _no_trades(svc) == []
    svc.no_trades_date = None
    svc.journal.db.execute("UPDATE orders SET ts=?", ((CLOSE - timedelta(days=20)).isoformat(),))
    svc.tick()
    assert "in 20 days" in _no_trades(svc)[0]


def test_blocked_orders_are_not_trades_and_the_note_says_so(svc):
    _after_close(svc, 30)
    _order(svc, "blocked", 2)
    _order(svc, "failed", 1)
    svc.tick()
    assert "tried 2 times" in _no_trades(svc)[0]


def test_no_note_when_holding_paused_turned_off_or_before_the_close(svc):
    _after_close(svc, 30)
    svc.broker.positions["VRT"] = Position("VRT", 1, 100, 100)
    _own(svc, "VRT")
    svc.broker.stops.add("VRT")
    svc.tick()
    svc.broker.positions.clear()
    svc.no_trades_date = None
    (svc.cfg.data_dir / "KILL").write_text("on")
    svc.tick()
    (svc.cfg.data_dir / "KILL").unlink()
    svc.no_trades_date, svc.kill_on = None, False
    svc.now = lambda: CLOSE - timedelta(hours=2)
    svc.tick()
    assert _no_trades(svc) == []
    svc.now = lambda: CLOSE
    svc.cfg = dataclasses.replace(svc.cfg, service=ServiceConfig(no_trades_days=0))
    svc.tick()
    assert _no_trades(svc) == []


def test_no_trades_days_is_checked(tmp_path):
    with pytest.raises(ConfigError, match="no_trades_days"):
        parse_config({"service": {"no_trades_days": 400}})


def test_open_orders_are_read_once_per_check(svc):
    svc.cfg.strategies[0] = dataclasses.replace(svc.cfg.strategies[0], symbols=["VRT", "NVDA", "AAPL"])
    svc.strategy_for.update({s: svc.strategy_for["VRT"] for s in ("NVDA", "AAPL")})
    svc.owner.update({s: svc.cfg.strategies[0] for s in ("NVDA", "AAPL")})
    calls = []
    real = svc.broker.open_orders
    svc.broker.open_orders = lambda: calls.append(1) or real()
    svc.broker.orders = [SimpleNamespace(symbol="NVDA", side=SimpleNamespace(value="buy"))]
    FLAGS["entry"] = True
    svc.tick()
    assert len(calls) == 1
    assert sorted(i.symbol for i in svc.broker.entries) == ["AAPL", "VRT"]  # NVDA already has a buy working


# --- review fixes ---------------------------------------------------------------------------------

def test_pause_pressed_mid_check_stops_the_buy(svc):
    FLAGS["entry"] = True
    real = svc.broker.snapshot

    def snapshot_then_pause():
        kill_file(svc.cfg).write_text("on")  # "sell everything" landing while the check runs
        return real()

    svc.broker.snapshot = snapshot_then_pause
    svc.tick()
    assert svc.broker.entries == []
    assert svc.journal.db.execute("SELECT status FROM orders").fetchone()["status"] == "blocked"


def test_shares_the_bot_didnt_buy_are_left_alone(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)  # bought by you, then VRT added to a strategy
    FLAGS["exit"] = True
    svc.tick()
    assert svc.broker.stops_placed == [] and svc.broker.closes == []
    assert any(t == "Not managing VRT" for t in svc.rec.titles())
    _own(svc, "VRT")  # once the bot's own buy is the last word, it manages the position
    svc.tick()
    assert svc.broker.stops_placed or svc.broker.closes


def test_a_critical_stop_alert_isnt_swallowed_by_a_routine_one(svc):
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    _own(svc, "VRT")
    svc.tick()  # stop recreated: a warning
    svc.broker.stops.clear()
    svc.broker.place_stop = lambda *a: (_ for _ in ()).throw(RuntimeError("rejected"))
    svc.tick()
    assert "Recreated stop: VRT" in svc.rec.titles() and "VRT has NO stop" in svc.rec.titles()


def test_no_stop_is_reported_when_auto_protect_is_off(svc):
    svc.cfg = dataclasses.replace(svc.cfg, service=ServiceConfig(auto_protect=False))
    svc.broker.positions["VRT"] = Position("VRT", 18, 1980, 100)
    _own(svc, "VRT")
    svc.tick()
    assert svc.broker.stops_placed == []
    assert any(t == "VRT has NO stop" and lvl == Level.CRITICAL for t, _, lvl in svc.rec.sent)


def test_an_order_alpaca_took_despite_an_error_is_recorded(svc):
    FLAGS["entry"] = True
    taken = SimpleNamespace(id="o-late")
    svc.broker.submit_entry = lambda intent, client_order_id=None: (_ for _ in ()).throw(TimeoutError("read timed out"))
    svc.broker.order_by_client_id = lambda cid: taken
    svc.tick()
    row = svc.journal.db.execute("SELECT status, broker_order_id FROM orders").fetchone()
    assert (row["status"], row["broker_order_id"]) == ("submitted", "o-late")
    assert "VRT" in svc.executor.inflight
