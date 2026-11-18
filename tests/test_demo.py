"""Demo mode: the whole dashboard on a made-up account, with no keys, no network and no Keychain."""

import socket
import subprocess
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from taylortrader import cli, demo, launchd
from taylortrader import secrets as secrets_mod
from taylortrader.web import api as api_mod
from taylortrader.web.api import create_app

WRITE = {"X-TaylorTrader": "1", "Origin": "http://127.0.0.1"}
# Every read the front end makes (dashboard/src), with the parameters it uses.
GETS = [
    "/api/setup", "/api/session", "/api/status", "/api/account", "/api/results", "/api/history", "/api/settings",
    "/api/performance?period=1M", "/api/performance?period=3M", "/api/performance?period=1A",
    "/api/orders?limit=40", "/api/events?limit=40", "/api/ai/decisions?limit=30", "/api/ai/grades",
    "/api/invest", "/api/invest/whatif?years=10", "/api/invest/whatif?years=5",
    "/api/chart?symbol=&period=1M", "/api/chart?symbol=VRT&period=1W", "/api/chart?symbol=NVDA&period=3M",
    "/api/chart?symbol=AMD&period=1A",
]


def _refuse(*a, **k):
    raise AssertionError("demo mode reached outside the process")


def seal(monkeypatch):
    """Any network connection, Keychain lookup, launchctl call or real Alpaca client fails the test."""
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(secrets_mod, "_read_keychain", _refuse)
    monkeypatch.setattr(secrets_mod, "keychain_available", _refuse)
    monkeypatch.setattr(api_mod, "keychain_available", _refuse)
    monkeypatch.setattr(launchd, "status", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr("taylortrader.broker.AlpacaBroker", _refuse)
    for name in (*secrets_mod.SETTABLE, secrets_mod.LIVE_CONFIRM_VAR):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def sealed(monkeypatch, tmp_path):
    seal(monkeypatch)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Building the made-up year takes about a second: once for the whole module, sealed too."""
    with pytest.MonkeyPatch.context() as mp:
        seal(mp)
        return demo.make_runtime(tmp_path_factory.mktemp("demo"))


@pytest.fixture
def client(built):
    c = TestClient(create_app(built, cache_ttl=0, project=built.config_path.parent), base_url="http://127.0.0.1")
    c.rt = built
    return c


@pytest.mark.parametrize("path", GETS)
def test_every_read_works(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text


def test_looks_like_a_running_paper_account(client):
    status = client.get("/api/status").json()
    assert status["demo"] and status["connected"] and not status["heartbeat"]["stale"]
    assert status["mode"] == "paper" and not status["dry_run"]
    account = client.get("/api/account").json()
    assert 8_000 < account["equity"] < 12_000 and account["cash"] > 0
    held = {p["symbol"]: p for p in account["positions"]}
    assert {"VRT", "NVDA", "AMD", "VTI", "VXUS", "BND"} <= set(held)
    assert all(held[s]["has_stop"] and held[s]["issues"] == [] for s in demo.STILL_OPEN)
    assert held["VTI"]["invest"] and not held["VTI"]["has_stop"]


def test_results_and_ai_panels_have_data(client):
    bot = client.get("/api/results").json()["bot"]
    assert 20 <= bot["trades"] <= 30 and 45 <= bot["win_rate"] <= 65
    assert len(client.get("/api/ai/decisions?limit=30").json()) == 30
    grades = client.get("/api/ai/grades").json()
    assert grades["graded"] >= 20
    assert client.get("/api/history").json()["closed_trades"] > 0  # the imported-trades panel shows
    chart = client.get("/api/chart?symbol=VRT&period=3M").json()
    assert chart["points"] and chart["marks"] and chart["position"]["stop"]
    perf = client.get("/api/performance?period=1A").json()
    assert len(perf["equity"]) > 200 and len(perf["benchmark_series"]) == len(perf["equity"])


def test_what_if_reaches_back_ten_years(client):
    mixes = client.get("/api/invest/whatif?years=10").json()["mixes"]
    assert all(m["result"] for m in mixes)
    assert [m["short"] for m in mixes if m["id"] == "conservative"] == [True]  # SGOV starts in 2020


def test_setup_shows_no_real_paths_or_secrets(client):
    s = client.get("/api/setup").json()
    assert s["config_path"] == "config.toml" and s["service"]["running"]
    assert s["secrets"]["ALPACA_PAPER_KEY_ID"] == "Keychain" and s["secrets"]["OPENAI_API_KEY"] is None
    assert "/Users/" not in str(s) and "/home/" not in str(s)


def test_backtest_runs_on_made_up_bars(client):
    r = client.post("/api/backtest", headers=WRITE, json={"strategy": "macd_rsi", "symbol": "VRT", "timeframe": "1Hour",
                                                          "days": 365})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["bars"] > 1000


def test_writes_are_harmless(client):
    broker = client.rt.broker
    r = client.post("/api/positions/sell", headers=WRITE, json={"symbol": "VRT"})
    assert r.status_code == 200 and ("sell", "VRT") in broker.requests
    assert {p["symbol"]: p["selling"] for p in client.get("/api/account").json()["positions"]}["VRT"]
    assert client.post("/api/positions/sell", headers=WRITE, json={"symbol": "VRT"}).status_code == 409
    assert client.post("/api/pause", headers=WRITE, json={"paused": True}).json()["kill_switch"]
    assert not client.post("/api/pause", headers=WRITE, json={"paused": False}).json()["kill_switch"]
    for method, path, body in [
        ("post", "/api/secrets", {"name": "NTFY_TOPIC", "value": "x" * 20}),
        ("delete", "/api/secrets/NTFY_TOPIC", None),
        ("post", "/api/alpaca/test", {"mode": "paper"}),
        ("post", "/api/alerts/test", None),
        ("post", "/api/ai/test", None),
        ("post", "/api/service", {"action": "stop"}),
        ("post", "/api/mode", {"mode": "dry-run"}),
    ]:
        r = client.request(method.upper(), path, headers=WRITE, json=body)
        assert r.status_code == 403 and "demo" in r.json()["detail"].lower(), path


def test_settings_save_to_the_temp_folder(client):
    s = client.get("/api/settings").json()
    cfg = s["config"]
    cfg["guardrails"]["max_trades_per_day"] = 8
    r = client.put("/api/settings", headers=WRITE, json={"config": cfg, "revision": s["revision"]})
    assert r.status_code == 200, r.text
    assert "max_trades_per_day = 8" in client.rt.config_path.read_text()


def test_prices_are_repeatable_and_add_up():
    md = demo.DemoMarketData(lambda: datetime(2026, 11, 13, 22, tzinfo=UTC))
    start = datetime(2026, 11, 2, tzinfo=UTC)
    daily = md.bars("VRT", "1Day", start)
    hourly = md.bars("VRT", "1Hour", start)
    minutes = md.bars("VRT", "1Min", start)
    assert daily.equals(md.bars("VRT", "1Day", start))
    assert len(daily) == 10 and len(hourly) == 60  # two weeks; six hourly bars a day (10:00-15:00)
    last_minute = minutes.groupby(minutes.index.tz_convert("America/New_York").date)["close"].last()
    assert list(last_minute.round(6)) == list(daily["close"].round(6))
    assert md.bars("ZZZZ", "5Min", start).shape[0] == 10 * 78  # any ticker works
    assert not md.bars("VRT", "1Min", datetime(2026, 11, 13, 21, tzinfo=UTC) + timedelta(days=1)).shape[0]
    assert isinstance(daily.index, pd.DatetimeIndex) and str(daily.index.tz) == "UTC"


def test_market_clock():
    ny = demo.NY
    opening = demo.market_clock(datetime(2026, 11, 16, 10, tzinfo=ny))  # a Monday morning
    assert opening.is_open and opening.next_close == datetime(2026, 11, 16, 16, tzinfo=ny)
    weekend = demo.market_clock(datetime(2026, 11, 14, 12, tzinfo=ny))
    assert not weekend.is_open and weekend.next_open == datetime(2026, 11, 16, 9, 30, tzinfo=ny)


def test_cli_demo_is_foreground_only(capsys, tmp_path):
    assert cli.main(["--config", str(tmp_path / "config.toml"), "dashboard", "--demo", "--background"]) == 2
    assert "terminal only" in capsys.readouterr().err


def test_daily_email_matches_the_dashboard(client):
    """demo.summary() is the service's own daily summary, from the same account the dashboard shows."""
    from taylortrader import report

    s = demo.summary(client.rt)
    assert s.day == demo.last_session(client.rt.broker.now()) and s.mode == "PAPER"
    assert len(s.closed_trades) == 3 and sum(t["pnl"] > 0 for t in s.closed_trades) == 2
    assert {t["symbol"] for t in s.closed_trades} == set(demo.LAST_SESSION_CLOSES)
    assert s.filled == {"buy": 1, "sell": 3} and {o["symbol"] for o in s.orders} >= {"AMD"}
    held = {p["symbol"] for p in client.get("/api/account").json()["positions"]}
    assert {p.symbol for p in s.positions} == held
    assert {p.symbol for p in s.positions if p.has_stop} == set(demo.STILL_OPEN)
    assert abs(s.equity - sum(p.value for p in s.positions) - s.cash) < 0.05
    # the AI calls behind the session's orders are the ones on the AI decisions panel
    ai = {(d["symbol"], d["action"]) for d in client.get("/api/ai/decisions?limit=30").json() if d["acted"]}
    assert ("AMD", "buy") in ai
    html = report.html(s)
    assert "AMD" in html and "/Users/" not in html


def test_last_session():
    ny = demo.NY
    assert demo.last_session(datetime(2026, 11, 17, 16, 30, tzinfo=ny)).isoformat() == "2026-11-17"
    assert demo.last_session(datetime(2026, 11, 17, 11, tzinfo=ny)).isoformat() == "2026-11-16"
    assert demo.last_session(datetime(2026, 11, 16, 9, tzinfo=ny)).isoformat() == "2026-11-13"  # Monday morning
