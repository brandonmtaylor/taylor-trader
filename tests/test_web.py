from datetime import UTC, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from guardrails import Limits, Position
from taylortrader.config import Config, ServiceConfig, StrategyConfig
from taylortrader.journal import Journal
from taylortrader.service import kill_file
from taylortrader.web.api import create_app
from tests.test_service import FakeBroker


class Bench:
    def bars(self, symbol, timeframe, start, end=None):
        idx = pd.date_range(start, periods=40, freq="D", tz="UTC")
        return pd.DataFrame({"close": range(500, 540)}, index=idx)


@pytest.fixture
def client(tmp_path):
    cfg = Config(data_dir=tmp_path, strategies=[StrategyConfig(name="macd_rsi", symbols=["VRT"])],
                 limits=Limits(), service=ServiceConfig())
    broker = FakeBroker(is_open=False)
    broker.positions = {"VRT": Position("VRT", 10, 2240, 315), "AAPL": Position("AAPL", 1, 330, 268)}
    broker.stops = {"VRT"}
    now = int(datetime.now(UTC).timestamp())
    broker.portfolio_history = lambda period: [(now - 86400 * i, 10_000 + i) for i in range(5, 0, -1)]
    j = Journal(tmp_path / "j.db")
    j.beat()
    c = TestClient(create_app(cfg, broker, Bench(), j, ["ntfy: OFF"], cache_ttl=0), base_url="http://127.0.0.1")
    c.cfg, c.broker = cfg, broker
    return c


def test_status(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "paper" and s["heartbeat"]["stale"] is False and s["kill_switch"] is False
    kill_file(client.cfg).write_text("on")
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


def test_read_only_and_local_only(client):
    assert client.post("/api/status").status_code == 405
    other = TestClient(client.app, base_url="http://evil.example.com")
    assert other.get("/api/status").status_code == 400  # DNS-rebinding guard


def test_broker_outage_is_a_clear_503(client):
    def down():
        raise ConnectionError("timeout")
    client.broker.snapshot = down
    r = client.get("/api/account")
    assert r.status_code == 503 and "Can't reach Alpaca" in r.json()["detail"]
