import tomllib
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from guardrails import AccountSnapshot, Limits, Position
from guardrails.invest import check_invest_buy
from taylortrader import invest
from taylortrader.alerts import Alerts
from taylortrader.config import (
    Config,
    ConfigError,
    InvestConfig,
    ServiceConfig,
    StrategyConfig,
    config_to_dict,
    load_config,
    parse_config,
    render_toml,
    save_config,
)
from taylortrader.journal import Journal
from taylortrader.service import Service
from tests.test_service import NOW, FakeBroker, FakeData, Recorder

THREE_FUND = [{"symbol": "VTI", "weight": 60}, {"symbol": "VXUS", "weight": 20}, {"symbol": "BND", "weight": 20}]


def plan(**kw):
    base = {"enabled": True, "preview": False, "amount": 200.0, "every": "month", "day": 1, "holdings": THREE_FUND}
    return InvestConfig(**{**base, **kw})


# --- settings -----------------------------------------------------------------------------------------

def test_plan_settings_validate_and_round_trip(tmp_path):
    raw = {"invest": {"enabled": True, "amount": 200, "holdings": [{"symbol": "vti", "weight": 60},
                                                                    {"symbol": "VXUS", "weight": 40}]}}
    cfg = parse_config(raw)
    assert cfg.invest.symbols == ["VTI", "VXUS"] and cfg.invest.preview is True  # preview unless turned off
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert load_config(path) == cfg
    assert "[invest]" in path.read_text()
    # A file with no plan doesn't grow an empty [invest] section.
    assert "[invest]" not in render_toml(config_to_dict(Config()))


@pytest.mark.parametrize(("block", "msg"), [
    ({"enabled": True, "amount": 200, "holdings": [{"symbol": "VTI", "weight": 60}]}, "add up to 60"),
    ({"enabled": True, "amount": 200, "holdings": []}, "at least one holding"),
    ({"enabled": True, "amount": 0.5, "holdings": THREE_FUND}, "at least $1"),
    ({"amount": 500_000, "holdings": THREE_FUND}, "limit per contribution"),
    ({"holdings": THREE_FUND, "every": "daily"}, "every must be"),
    ({"holdings": THREE_FUND, "day": 31}, "1-28"),
    ({"holdings": THREE_FUND, "every": "week", "day": 5}, "Friday"),
    ({"holdings": THREE_FUND, "start": "2027-01-01", "until": "2026-01-01"}, "before start"),
    ({"holdings": [{"symbol": "VTI", "weight": 50}, {"symbol": "VTI", "weight": 50}]}, "twice"),
    ({"holdings": [{"symbol": "BTC/USD", "weight": 100}]}, "stock or ETF"),
])
def test_bad_plans_are_refused(block, msg):
    with pytest.raises(ConfigError, match=msg.replace("$", r"\$")):
        parse_config({"invest": block})


def test_a_symbol_cant_be_in_the_plan_and_a_strategy():
    raw = {"strategies": [{"name": "macd_rsi", "symbols": ["VTI"]}], "invest": {"holdings": THREE_FUND}}
    with pytest.raises(ConfigError, match="one owner"):
        parse_config(raw)


# --- schedule -----------------------------------------------------------------------------------------

def test_monthly_is_due_from_its_day_until_the_month_ends():
    p = plan(day=5)
    assert invest.due_period(p, date(2026, 11, 4)) is None
    assert invest.due_period(p, date(2026, 11, 5)) == "2026-11"
    assert invest.due_period(p, date(2026, 11, 30)) == "2026-11"  # missed days catch up
    assert invest.due_period(p, date(2026, 12, 1)) is None


def test_weekly_two_weekly_once_start_and_until():
    wk = plan(every="week", day=2)  # Wednesdays
    assert invest.due_period(wk, date(2026, 11, 3)) is None            # Tuesday
    assert invest.due_period(wk, date(2026, 11, 4)) == "week-2026-11-02"
    assert invest.due_period(wk, date(2026, 11, 6)) == "week-2026-11-02"
    two = plan(every="2weeks", day=0)
    periods = {invest.due_period(two, date(2026, 11, 2) + timedelta(days=k)) for k in range(28)} - {None}
    assert len(periods) == 2
    assert invest.due_period(plan(every="once"), date(2026, 11, 4)) == "once"
    assert invest.due_period(plan(start="2026-12-01"), date(2026, 11, 20)) is None
    assert invest.due_period(plan(until="2026-10-31"), date(2026, 11, 20)) is None


def test_next_due_skips_done_periods_and_weekends():
    p = plan(day=1)
    assert invest.next_due(p, date(2026, 11, 4), set()) == (date(2026, 11, 4), "2026-11")
    assert invest.next_due(p, date(2026, 11, 4), {"2026-11"}) == (date(2026, 12, 1), "2026-12")
    # Aug 1 2026 is a Saturday: the next weekday.
    assert invest.next_due(p, date(2026, 7, 20), {"2026-07"}) == (date(2026, 8, 3), "2026-08")
    assert invest.next_due(plan(until="2026-11-30"), date(2026, 11, 4), {"2026-11"}) is None


# --- the math -------------------------------------------------------------------------------------------

def test_first_contribution_follows_the_weights():
    out = invest.split(200, {}, {"VTI": 60, "VXUS": 20, "BND": 20})
    assert out == {"VTI": 120.0, "VXUS": 40.0, "BND": 40.0}


def test_new_money_goes_to_whats_under_its_weight_first():
    # VTI ran up: 70% of the plan instead of 60. New money should skip it.
    values = {"VTI": 700.0, "VXUS": 150.0, "BND": 150.0}
    out = invest.split(100, values, {"VTI": 60, "VXUS": 20, "BND": 20})
    assert "VTI" not in out
    assert out["VXUS"] == out["BND"] == 50.0
    after = {s: values[s] + out.get(s, 0) for s in values}
    target_total = sum(after.values())
    for s, w in {"VTI": 60, "VXUS": 20, "BND": 20}.items():  # never pushes anything over its target
        assert after[s] <= target_total * w / 100 + 0.01 or s == "VTI"


def test_split_is_whole_cents_adds_up_and_folds_tiny_orders():
    out = invest.split(100, {}, {"A": 33.3, "B": 33.3, "C": 33.4})
    assert round(sum(out.values()), 2) == 100.0
    assert all(round(v, 2) == v for v in out.values())
    tiny = invest.split(10, {}, {"VTI": 95, "BND": 5})  # BND's 50 cents is under the $1 minimum
    assert tiny == {"VTI": 10.0}
    assert invest.split(0.5, {}, {"VTI": 100}) == {}


def test_drift_reports_points_off_target():
    d = invest.drift({"VTI": 700, "VXUS": 150, "BND": 150}, {"VTI": 60, "VXUS": 20, "BND": 20})
    assert d["VTI"]["drift"] == pytest.approx(10)
    assert d["BND"]["drift"] == pytest.approx(-5)


# --- guardrails -------------------------------------------------------------------------------------------

def test_invest_guardrails():
    acct = AccountSnapshot(equity=1000, cash=150, day_start_equity=1100)  # down 9% today: doesn't matter
    ok = {"plan_symbols": ["VTI"], "max_notional": 200, "kill_switch": False}
    assert check_invest_buy("VTI", 100, acct, **ok)
    assert not check_invest_buy("VTI", 100, acct, **{**ok, "kill_switch": True})
    assert not check_invest_buy("NVDA", 100, acct, **ok)
    assert not check_invest_buy("VTI", 0.5, acct, **ok)
    assert not check_invest_buy("VTI", 250, AccountSnapshot(1000, 1000, 1000), **ok)  # over the contribution
    assert not check_invest_buy("VTI", 151, acct, **ok)                                # no margin
    assert not check_invest_buy("VTI", float("nan"), acct, **ok)


# --- running it in the service ----------------------------------------------------------------------------

class InvestBroker(FakeBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.by_cid, self.notional_buys = {}, []
        self.assets = {}
        self.fail_next = None

    def submit_notional_buy(self, symbol, notional, cid):
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            if e == "timeout-but-placed":
                self.by_cid[cid] = SimpleNamespace(id=f"n-{cid}")
                self.notional_buys.append((symbol, notional))
                raise TimeoutError("read timed out")
            raise RuntimeError(e)
        if cid in self.by_cid:
            raise RuntimeError("client_order_id must be unique")
        self.by_cid[cid] = SimpleNamespace(id=f"n-{cid}")
        self.notional_buys.append((symbol, notional))
        return self.by_cid[cid]

    def order_by_client_id(self, cid):
        return self.by_cid.get(cid)

    def asset_status(self, symbol):
        return self.assets.get(symbol, (True, True))


def make(tmp_path, inv=None, strategies=None, broker=None, now=NOW):
    cfg = Config(data_dir=tmp_path, strategies=strategies or [], limits=Limits(), service=ServiceConfig(),
                 invest=inv or plan())
    rec = Recorder()
    s = Service(cfg, broker or InvestBroker(), FakeData(), Journal(tmp_path / "j.db"), Alerts(push=[rec], email=[rec]),
                now=lambda: now, sleep=lambda s: None)
    s.rec = rec
    return s


def test_a_due_contribution_is_bought_once_and_journaled(tmp_path):
    s = make(tmp_path)
    s.tick()
    assert sorted(s.broker.notional_buys) == [("BND", 40.0), ("VTI", 120.0), ("VXUS", 40.0)]
    assert any("Invest plan: buying $200.00" in t for t in s.rec.titles())
    orders = s.journal.recent_orders()
    assert {o["strategy"] for o in orders} == {"invest"} and all(o["status"] == "submitted" for o in orders)
    s.tick()
    s.tick()
    assert len(s.broker.notional_buys) == 3                 # not again this month
    # Even a fresh service (restart) on the same journal doesn't buy again.
    s2 = make(tmp_path, broker=s.broker)
    s2.tick()
    assert len(s.broker.notional_buys) == 3
    assert s.journal.invest_put_in("paper") == pytest.approx(200)


def test_plan_fills_are_recorded_quietly(tmp_path):
    s = make(tmp_path)
    s.tick()
    for o in s.journal.recent_orders():
        s.broker.order_status[o["broker_order_id"]] = SimpleNamespace(
            status="filled", filled_avg_price="100", filled_at="2026-11-04T15:00:01Z", filled_qty="0.4")
    before = len(s.rec.sent)
    s.check_fills()
    assert all(o["status"] == "filled" for o in s.journal.recent_orders())
    assert len(s.rec.sent) == before


def test_crash_between_sending_and_recording_doesnt_buy_twice(tmp_path):
    s = make(tmp_path)
    s.broker.fail_next = "timeout-but-placed"   # Alpaca got the first order, but the reply was lost
    s.tick()
    assert len(s.broker.notional_buys) == 3      # the "failed" one was found by its client order id
    assert all(r["status"] == "submitted" for r in s.journal.invest_rows("2026-11", "paper"))


def test_a_real_failure_retries_with_a_new_order_id_then_gives_up(tmp_path):
    s = make(tmp_path)
    s.broker.fail_next = "insufficient buying power"
    s.tick()
    rows = {r["symbol"]: r for r in s.journal.invest_rows("2026-11", "paper")}
    failed = [r for r in rows.values() if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["attempts"] == 1
    s.tick()  # retried and succeeds
    assert all(r["status"] == "submitted" for r in s.journal.invest_rows("2026-11", "paper"))
    assert len(s.broker.notional_buys) == 3


def test_preview_alerts_without_ordering_and_doesnt_use_up_the_month(tmp_path):
    s = make(tmp_path, inv=plan(preview=True))
    s.tick()
    assert s.broker.notional_buys == []
    assert any("[preview] Invest plan would buy $200.00" in t for t in s.rec.titles())
    live = make(tmp_path, inv=plan(preview=False), broker=s.broker)
    live.tick()  # turning preview off buys this month for real
    assert len(s.broker.notional_buys) == 3


def test_not_before_ten_not_when_off_not_when_not_due(tmp_path):
    early = make(tmp_path / "a", now=NOW - timedelta(minutes=1))   # 9:59 New York
    early.tick()
    assert early.broker.notional_buys == []
    off = make(tmp_path / "b", inv=plan(enabled=False))
    off.tick()
    assert off.broker.notional_buys == []
    later = make(tmp_path / "c", inv=plan(day=20))
    later.tick()
    assert later.broker.notional_buys == []


def test_short_cash_invests_what_there_is_and_says_so(tmp_path):
    b = InvestBroker()
    b.positions = {"X": Position("X", 1, 9_880.0, 9_880.0)}  # leaves $120 cash
    s = make(tmp_path, broker=b)
    s.tick()
    assert sum(n for _, n in b.notional_buys) == pytest.approx(120)
    assert any("Only $120.00 of $200.00" in m for _, m, _ in s.rec.sent)


def test_kill_switch_and_unbuyable_symbols_hold_the_plan(tmp_path):
    s = make(tmp_path / "a")
    s.guardrails.kill_switch = True
    s.invest.tick(s.broker.snapshot())
    assert s.broker.notional_buys == [] and "Invest plan waiting" in s.rec.titles()
    s.guardrails.kill_switch = False
    s.invest.tick(s.broker.snapshot())
    assert len(s.broker.notional_buys) == 3   # goes in once it's off

    b = InvestBroker()
    b.assets["BND"] = (True, False)
    s2 = make(tmp_path / "b", broker=b)
    s2.tick()
    assert b.notional_buys == [] and "Invest plan can't buy" in s2.rec.titles()


def test_plan_holdings_need_no_stop_and_strategies_size_from_the_trading_slice(tmp_path):
    b = InvestBroker()
    b.positions = {"VTI": Position("VTI", 10, 3_000.0, 290.0)}
    sc = StrategyConfig(name="test_signal", symbols=["VRT"], timeframe="5Min", allocation_pct=20, stop_loss_pct=10)
    s = make(tmp_path, strategies=[sc], broker=b, inv=plan(day=20))  # not due today
    s.tick()
    assert b.stops_placed == [] and not any("Unmanaged" in t for t in s.rec.titles())
    # Trading slice = $10,000 - $3,000 plan holdings - $200 next contribution = $6,800; 20% = $1,360.
    from tests.test_service import FLAGS

    FLAGS.update(entry=True)
    try:
        s.last_bar.clear()
        s.tick()
    finally:
        FLAGS.update(entry=False)
    intent = b.entries[-1]
    assert intent.qty * intent.est_price <= 1_360 and (intent.qty + 1) * intent.est_price > 1_360


def test_daily_summary_has_a_plan_line_and_says_when_buys_were_blocked(tmp_path):
    s = make(tmp_path)
    s.tick()
    summary = s.daily_summary(s.broker.snapshot())
    assert any(n.startswith("Invest plan: $200.00 put in") and "Dec 1" in n for n in summary.notes)
    from taylortrader import report

    blocked = replace(summary, day_pnl_pct=-4.2)
    assert "blocked today (down 4.2%, daily limit 3%); allowed again tomorrow" in report.text(blocked)
    assert "Blocked today" in report.html(blocked)


def test_config_toml_written_by_hand_parses():
    text = """
[invest]
enabled = true
preview = true
amount = 200
every = "month"
day = 1
holdings = [
  { symbol = "VTI", weight = 60 },
  { symbol = "VXUS", weight = 20 },
  { symbol = "BND", weight = 20 },
]
"""
    cfg = parse_config(tomllib.loads(text))
    assert cfg.invest.weights == {"VTI": 60, "VXUS": 20, "BND": 20}


def test_next_buy_after_the_close_is_the_next_open_day(tmp_path):
    evening = NOW.replace(hour=23)  # Wed 6pm New York
    s = make(tmp_path, now=evening)
    assert s.invest.status(s.broker.snapshot())["next"]["date"] == "2026-11-05"
    assert make(tmp_path / "b").invest.status(s.broker.snapshot())["next"]["date"] == "2026-11-04"


# --- review fixes (2026-11-06) ---------------------------------------------------------------------

def test_a_missed_day_catches_up_across_the_boundary_only_after_the_plan_started():
    feb28_saturday = plan(day=28, start="2026-01-01")
    assert invest.due_period(feb28_saturday, date(2026, 3, 2)) == "2026-02"       # bought on Monday
    assert invest.due_period(plan(day=28, start="2026-03-01"), date(2026, 3, 2)) is None  # plan began after
    assert invest.due_period(plan(day=28), date(2026, 3, 2)) is None               # no start: no catch-up
    friday = plan(every="week", day=4, start="2026-01-01")
    assert invest.due_period(friday, date(2026, 12, 28)) == "week-2026-12-21"      # Christmas Friday
    two = plan(every="2weeks", day=4, start="2026-01-01")
    p = invest.due_period(two, date(2026, 11, 2))
    assert p and p.startswith("2weeks-")


def test_a_new_lump_sum_is_a_new_period():
    assert invest.due_period(plan(every="once", start="2026-11-01"), date(2026, 11, 2)) == "once-2026-11-01"
    assert invest.due_period(plan(every="once", start="2027-11-01"), date(2027, 11, 2)) == "once-2027-11-01"


def test_cents_dont_lose_a_penny_to_float_noise():
    assert invest.cents(4.35) == 4.35 and invest.cents(0.29) == 0.29 and invest.cents(1.999) == 1.99
    assert sum(invest.split(4.35, {}, {"VTI": 100}).values()) == 4.35


def test_an_unconfirmed_buy_is_retried_with_the_same_order_id(tmp_path):
    s = make(tmp_path)
    s.broker.fail_next = "timeout-but-placed"     # Alpaca got it, the reply was lost...
    real_lookup = s.broker.order_by_client_id

    def lookup_down(cid):                            # ...and the check fails too
        s.broker.order_by_client_id = real_lookup
        raise ConnectionError("network down")

    s.broker.order_by_client_id = lookup_down
    s.tick()
    s.tick()
    assert len(s.broker.notional_buys) == 3          # never twice
    assert all(cid.endswith("-1") for cid in s.broker.by_cid)
    assert all(r["status"] == "submitted" for r in s.journal.invest_rows("2026-11", "paper"))


def test_a_blocked_buy_goes_in_once_the_block_clears(tmp_path):
    s = make(tmp_path)
    s.journal.plan_invest("2026-11", "paper", {"VTI": 200.0}, planned_total=200.0)
    row = s.journal.invest_rows("2026-11", "paper")[0]
    s.journal.invest_mark(row["id"], "blocked", "kill switch")
    s.tick()
    assert s.broker.notional_buys == [("VTI", 200.0)]
