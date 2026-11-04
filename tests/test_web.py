from datetime import UTC, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from guardrails import Position
from taylortrader import secrets as secrets_mod
from taylortrader.config import Config, StrategyConfig, load_config, save_config
from taylortrader.service import kill_file
from taylortrader.web import api as api_mod
from taylortrader.web.api import create_app
from taylortrader.web.runtime import Runtime
from tests.test_service import FakeBroker

WRITE = {"X-TaylorTrader": "1", "Origin": "http://127.0.0.1"}


class Bench:
    def bars(self, symbol, timeframe, start, end=None):
        idx = pd.date_range(start, periods=40, freq="D", tz="UTC")
        return pd.DataFrame({"close": range(500, 540)}, index=idx)


class FakeAlpaca:
    """Stands in for AlpacaBroker in the key test: accepts keys that start with 'good'."""

    def __init__(self, creds):
        self.creds = creds

    def account_info(self):
        if not self.creds.key_id.get_secret_value().startswith("good"):
            raise RuntimeError('{"code":40110000,"message":"request is not authorized"}')
        return {"account": "…1234", "status": "ACTIVE", "equity": 5000.0, "paper": self.creds.paper}


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch, tmp_path):
    """Tests never touch the real Keychain or environment: secrets go to a temp .env."""
    monkeypatch.setattr(secrets_mod, "_read_keychain", lambda *a, **k: None)
    monkeypatch.setattr(secrets_mod, "keychain_available", lambda: False)
    monkeypatch.setattr(api_mod, "keychain_available", lambda: False)
    monkeypatch.setattr("taylortrader.broker.AlpacaBroker", FakeAlpaca)
    for name in (*secrets_mod.SETTABLE, secrets_mod.LIVE_CONFIRM_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def make_client(tmp_path, cfg: Config | None):
    broker = FakeBroker(is_open=False)
    broker.positions = {"VRT": Position("VRT", 10, 2240, 315), "AAPL": Position("AAPL", 1, 330, 268)}
    broker.stops = {"VRT"}
    now = int(datetime.now(UTC).timestamp())
    broker.portfolio_history = lambda period: [(now - 86400 * i, 10_000 + i) for i in range(5, 0, -1)]
    path = tmp_path / "config.toml"
    if cfg is not None:
        save_config(cfg, path)

    def connect(cfg, dotenv):
        secrets_mod.load_alpaca_credentials(cfg.mode, dotenv)  # same key checks as the real thing
        return broker, Bench(), ["ntfy: OFF"]

    rt = Runtime(config_path=path, dotenv_path=tmp_path / ".env", connect=connect)
    c = TestClient(create_app(rt, cache_ttl=0), base_url="http://127.0.0.1")
    c.rt, c.broker, c.path = rt, broker, path
    return c


@pytest.fixture
def client(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ALPACA_PAPER_KEY_ID=goodpaper\nALPACA_PAPER_SECRET_KEY=s3cretpaper\n")
    cfg = Config(data_dir=tmp_path / "var", strategies=[StrategyConfig(name="macd_rsi", symbols=["VRT"])])
    c = make_client(tmp_path, cfg)
    c.rt.journal.beat()
    return c


# --- reads ---------------------------------------------------------------------------------------

def test_status(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "paper" and s["heartbeat"]["stale"] is False and s["kill_switch"] is False
    assert s["connected"] and s["mode_label"] == "paper"
    kill_file(client.rt.settings).write_text("on")
    assert client.get("/api/status").json()["kill_switch"] is True


def test_account_flags_unprotected_and_unmanaged(client):
    a = client.get("/api/account").json()
    by = {p["symbol"]: p for p in a["positions"]}
    assert by["VRT"]["has_stop"] and by["VRT"]["owner"] == "macd_rsi" and by["VRT"]["issues"] == []
    assert not by["AAPL"]["has_stop"] and len(by["AAPL"]["issues"]) == 2
    assert by["VRT"]["unrealized_pct"] == pytest.approx((224 / 315 - 1) * 100)


def test_performance_includes_benchmark(client):
    p = client.get("/api/performance?period=1M").json()
    assert len(p["equity"]) == 5 and len(p["benchmark_series"]) == 5
    assert client.get("/api/performance?period=bogus").status_code == 422


def test_broker_outage_is_a_clear_503(client):
    def down():
        raise ConnectionError("timeout")
    client.broker.snapshot = down
    r = client.get("/api/account")
    assert r.status_code == 503 and "Can't reach Alpaca" in r.json()["detail"]


def test_setup_reports_where_secrets_are_never_values(client):
    s = client.get("/api/setup").json()
    assert s["configured"] and s["connected"] and s["secret_store"] == ".env"
    assert s["secrets"]["ALPACA_PAPER_KEY_ID"] == ".env" and s["secrets"]["NTFY_TOPIC"] is None
    assert "goodpaper" not in client.get("/api/setup").text and "s3cret" not in client.get("/api/settings").text


# --- write protection -----------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {},                                                          # plain form post from anywhere
    {"Origin": "http://127.0.0.1"},                              # missing the custom header
    {"X-TaylorTrader": "1"},                                     # no Origin
    {"X-TaylorTrader": "1", "Origin": "https://evil.example"},   # another website
    {"X-TaylorTrader": "1", "Origin": "http://127.0.0.1:9999"},  # another local port
])
def test_writes_need_header_and_same_origin(client, headers):
    r = client.post("/api/pause", json={"paused": True}, headers=headers)
    assert r.status_code == 403
    assert not kill_file(client.rt.settings).exists()


def test_other_hostnames_are_refused(client):
    other = TestClient(client.app, base_url="http://evil.example.com")
    assert other.get("/api/status").status_code == 400  # DNS-rebinding guard


# --- settings --------------------------------------------------------------------------------------

def test_save_settings_validates_writes_and_keeps_backup(client):
    cfg = client.get("/api/settings").json()["config"]
    cfg["strategies"].append({"name": "rsi_14", "symbols": ["amd"], "timeframe": "5Min",
                              "allocation_pct": 10, "stop_loss_pct": 8})
    cfg["mode"], cfg["dry_run"] = "live", False  # ignored: mode only changes through /api/mode
    r = client.put("/api/settings", json={"config": cfg}, headers=WRITE)
    assert r.status_code == 200, r.text
    saved = load_config(client.path)
    assert [s.symbols for s in saved.strategies] == [["VRT"], ["AMD"]] and saved.mode == "paper"
    assert (client.path.parent / "config.toml.bak").exists()


@pytest.mark.parametrize(("change", "message"), [
    (lambda c: c["strategies"][0].update(allocation_pct=40), "position size"),
    (lambda c: c["strategies"][0].update(stop_loss_pct=30), "stop-loss"),
    (lambda c: c["strategies"][0].update(symbols=["NOT A TICKER"]), "ticker"),
    (lambda c: c["strategies"][0].update(name="nope"), "unknown strategy"),
    (lambda c: c["guardrails"].update(max_daily_loss_pct=20), "hard limit"),
    (lambda c: c["alerts"].update(api_key="x"), "looks like a secret"),
    (lambda c: c["strategies"].append(dict(c["strategies"][0])), "used by both"),
])
def test_bad_settings_are_refused_and_nothing_is_written(client, change, message):
    before = client.path.read_text()
    cfg = client.get("/api/settings").json()["config"]
    change(cfg)
    r = client.put("/api/settings", json={"config": cfg}, headers=WRITE)
    assert r.status_code == 422 and message in r.json()["detail"]
    assert client.path.read_text() == before


def test_first_run_works_without_settings_or_keys(tmp_path):
    c = make_client(tmp_path, None)
    s = c.get("/api/setup").json()
    assert not s["configured"] and not s["connected"] and s["secrets"]["ALPACA_PAPER_KEY_ID"] is None
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/account").status_code == 503
    cfg = c.get("/api/settings").json()["config"]
    assert cfg["dry_run"] is True and cfg["feed"] == "yahoo"
    cfg["strategies"] = [{"name": "macd_rsi", "symbols": ["VRT"], "timeframe": "5Min",
                          "allocation_pct": 20, "stop_loss_pct": 12}]
    assert c.put("/api/settings", json={"config": cfg}, headers=WRITE).status_code == 200
    assert load_config(c.path).dry_run is True  # a brand-new setup starts in dry run


# --- keys --------------------------------------------------------------------------------------------

def test_keys_are_tested_then_stored_and_never_echoed(client):
    bad = client.post("/api/alpaca/test", json={"mode": "paper", "key_id": "bad", "secret_key": "x"}, headers=WRITE)
    assert bad.status_code == 422 and "didn't accept" in bad.json()["detail"]
    ok = client.post("/api/alpaca/test", json={"mode": "paper", "key_id": "goodnew", "secret_key": "zz9"},
                     headers=WRITE)
    assert ok.json()["account"] == "…1234" and "goodnew" not in ok.text
    r = client.post("/api/secrets", json={"name": "NTFY_TOPIC", "value": "my-topic-123"}, headers=WRITE)
    assert r.json()["secrets"]["NTFY_TOPIC"] == ".env" and "my-topic-123" not in r.text
    assert client.post("/api/secrets", json={"name": "PATH", "value": "x"}, headers=WRITE).status_code == 422
    assert client.post("/api/secrets", json={"name": secrets_mod.LIVE_CONFIRM_VAR, "value": "1"},
                       headers=WRITE).status_code == 422  # only the go-live flow sets this
    client.delete("/api/secrets/NTFY_TOPIC", headers=WRITE)
    assert client.get("/api/setup").json()["secrets"]["NTFY_TOPIC"] is None


# --- mode ----------------------------------------------------------------------------------------------

def test_dry_run_and_paper_switch_freely(client):
    s = client.post("/api/mode", json={"mode": "dry-run"}, headers=WRITE).json()
    assert s["dry_run"] and s["mode_label"] == "dry run (paper)" and load_config(client.path).dry_run
    s = client.post("/api/mode", json={"mode": "paper"}, headers=WRITE).json()
    assert not s["dry_run"] and s["mode"] == "paper"


def test_live_needs_phrase_and_working_live_keys(client):
    r = client.post("/api/mode", json={"mode": "live", "confirm": "yes"}, headers=WRITE)
    assert r.status_code == 422 and "GO LIVE" in r.json()["detail"]
    r = client.post("/api/mode", json={"mode": "live", "confirm": "GO LIVE"}, headers=WRITE)
    assert r.status_code == 422 and "No live keys" in r.json()["detail"]
    assert load_config(client.path).mode == "paper"

    with (client.path.parent / ".env").open("a") as f:
        f.write("ALPACA_LIVE_KEY_ID=goodlive\nALPACA_LIVE_SECRET_KEY=s3cretlive\n")
    r = client.post("/api/mode", json={"mode": "live", "confirm": "GO LIVE"}, headers=WRITE)
    assert r.status_code == 200, r.text
    assert r.json()["mode_label"] == "LIVE" and load_config(client.path).mode == "live"
    assert secrets_mod.live_trading_confirmed(client.path.parent / ".env")
    events = [e["message"] for e in client.get("/api/events").json()]
    assert any("paper -> LIVE" in m for m in events)

    client.post("/api/mode", json={"mode": "paper"}, headers=WRITE)
    assert not secrets_mod.live_trading_confirmed(client.path.parent / ".env")  # flag removed again


def test_pause_and_resume(client):
    assert client.post("/api/pause", json={"paused": True}, headers=WRITE).json()["kill_switch"] is True
    assert client.post("/api/pause", json={"paused": False}, headers=WRITE).json()["kill_switch"] is False


def test_ai_decisions_and_backtest_refusal(client):
    client.rt.journal.record_ai(strategy="ai", symbol="VRT", question="entry", action="hold", confidence=0.4,
                                summary="Wait.", pros=["cheap"], cons=["falling"], model="m")
    rows = client.get("/api/ai/decisions").json()
    assert rows[0]["summary"] == "Wait." and rows[0]["cons"] == ["falling"]
    cat = {c["name"]: c for c in client.get("/api/settings").json()["catalog"]}
    assert cat["ai"]["backtestable"] is False and "instructions" in cat["ai"]["params"]
    assert client.get("/api/settings").json()["config"]["ai"]["provider"] == "anthropic"


def test_ai_test_reports_missing_setup(client, monkeypatch):
    from taylortrader import llm

    def build(*a, **k):
        raise llm.LLMError("Add an OpenAI API key, or set an endpoint for a local model (Settings → AI)")
    monkeypatch.setattr(llm, "build", build)
    r = client.post("/api/ai/test", headers=WRITE)
    assert r.status_code == 422 and "API key" in r.json()["detail"]


# --- selling by hand and results -------------------------------------------------------------------

def test_sell_one_position_is_journaled(client):
    r = client.post("/api/positions/sell", json={"symbol": "aapl"}, headers=WRITE)
    assert r.status_code == 200 and r.json()["symbol"] == "AAPL"
    assert client.broker.closes == ["AAPL"]
    row = client.rt.journal.recent_orders(1)[0]
    assert row["side"] == "sell" and row["strategy"] == "dashboard" and row["broker_order_id"] == "c1"
    assert client.post("/api/positions/sell", json={"symbol": "TSLA"}, headers=WRITE).status_code == 404
    client.broker.selling = {"AAPL"}  # the sell is waiting for the open
    assert client.post("/api/positions/sell", json={"symbol": "AAPL"}, headers=WRITE).status_code == 409
    rows = {p["symbol"]: p for p in client.get("/api/account").json()["positions"]}
    assert rows["AAPL"]["selling"] is True and rows["VRT"]["selling"] is False
    assert client.post("/api/positions/sell", json={"symbol": "VRT"}).status_code == 403  # writes need the header


def test_sell_everything_needs_phrase_pauses_and_keeps_going(client):
    cancelled = []  # how many sells had gone out when the cancel ran
    client.broker.cancel_all_orders = lambda: cancelled.append(len(client.broker.closes))
    assert client.post("/api/positions/sell-all", json={"confirm": "yes"}, headers=WRITE).status_code == 422
    assert client.broker.closes == [] and not kill_file(client.rt.settings).exists()

    real_close = client.broker.close_position

    def close(symbol, reason=""):
        if symbol == "AAPL":
            raise RuntimeError("insufficient qty")
        return real_close(symbol, reason)
    client.broker.close_position = close
    out = client.post("/api/positions/sell-all", json={"confirm": "SELL ALL"}, headers=WRITE).json()
    assert [s["symbol"] for s in out["sold"]] == ["VRT"] and out["failed"][0]["symbol"] == "AAPL"
    # Cancel before selling: afterwards it would cancel the sells queued for the next open.
    assert kill_file(client.rt.settings).exists() and cancelled == [0]


def test_results_pair_fills_and_credit_the_strategy(client):
    from datetime import timedelta

    from taylortrader.stats import Fill

    t0 = datetime.now(UTC) - timedelta(days=3)
    client.rt.journal.record_order(mode="paper", strategy="macd_rsi", symbol="VRT", side="buy", status="filled",
                                   broker_order_id="b1")
    client.broker.fills = lambda: [
        Fill(t0, "VRT", "buy", 10, 100.0, "b1"),
        Fill(t0 + timedelta(days=1), "VRT", "sell", 10, 110.0, "s1"),       # bot trade: +$100
        Fill(t0, "AAPL", "buy", 2, 200.0, "manual"),
        Fill(t0 + timedelta(hours=2), "AAPL", "sell", 2, 190.0, "s2"),       # hand trade: -$20
        Fill(t0 - timedelta(days=1), "MSFT", "sell", 1, 400.0, "s3"),        # opened before the window: skipped
    ]
    r = client.get("/api/results").json()
    assert r["bot"]["trades"] == 1 and r["bot"]["realized"] == pytest.approx(100)
    assert r["bot"]["streak"] == {"kind": "W", "n": 1} and r["bot"]["profit_factor"] is None
    assert r["by_strategy"][0]["strategy"] == "macd_rsi"
    assert r["other"] == {"trades": 1, "realized": pytest.approx(-20)}


def test_page_is_never_served_stale(client):
    assert client.get("/api/status").headers["cache-control"] == "no-cache"
