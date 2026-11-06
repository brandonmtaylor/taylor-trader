"""User mistakes on the command line get a clear message and exit code 2, not a traceback."""

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from taylortrader import cli, data, launchd, replay, search

N = 900


def bars(n=N, end="2026-11-02 20:00"):
    idx = pd.date_range(end=end, periods=n, freq="h", tz="UTC")
    c = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, n)))
    return pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1e6}, index=idx)


@pytest.fixture
def market(monkeypatch, tmp_path):
    """No keys, no network: `frame` is what every symbol's download returns."""
    state = SimpleNamespace(frame=bars())

    class FakeData:
        feed = "sip"

        def __init__(self, *a, **kw):
            pass

        def bars(self, symbol, timeframe, start):
            return state.frame

    ai = SimpleNamespace(name="ai", timeframe="15Min", symbols=["NVDA"], params={"entry_when": "rsi < 50"})
    cfg = SimpleNamespace(feed="sip", data_dir=tmp_path, strategies=[ai],
                          ai=SimpleNamespace(provider="p", model="m", effort="low", endpoint="", timeout_seconds=1))
    monkeypatch.setattr(data, "MarketData", FakeData)
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "load_alpaca_credentials", lambda mode: None)
    return state


def run(*argv):
    return cli.main(["--config", "unused.toml", *argv])


def test_backtest_all_skips_what_cant_be_backtested_and_says_so(market, capsys):
    assert run("backtest", "VRT", "--all", "--timeframe", "1Hour") == 0
    out = capsys.readouterr().out
    assert "Skipping ai" in out and "macd_rsi" in out


@pytest.mark.parametrize(("args", "message"), [
    (["--strategy", "nope"], "unknown strategy 'nope'"),
    (["--strategy", "ai"], "can't be backtested honestly"),
    (["--param", "nope=1"], "unknown parameter"),
])
def test_backtest_refuses_bad_strategies_clearly(market, capsys, args, message):
    assert run("backtest", "VRT", *args) == 2
    assert message in capsys.readouterr().err


def test_backtest_with_no_bars(market, capsys):
    market.frame = bars().iloc[0:0]
    assert run("backtest", "VRT") == 2
    assert "no 5Min bars for VRT" in capsys.readouterr().err


def test_backtest_with_too_few_bars(market, capsys):
    market.frame = bars(n=20)
    assert run("backtest", "VRT") == 2
    assert "need more than" in capsys.readouterr().err


@pytest.mark.parametrize("stops", ["", "8,", "eight"])
def test_search_refuses_bad_stops(market, capsys, stops):
    assert run("search", "VRT", f"--stops={stops}") == 2
    assert "--stops needs percentages" in capsys.readouterr().err


def test_search_with_no_usable_symbols(market, capsys):
    market.frame = bars(n=10)
    assert run("search", "VRT") == 2
    assert "no symbol had enough history" in capsys.readouterr().err


def test_search_seeds_0_still_prints_a_verdict(market, capsys, monkeypatch):
    monkeypatch.setattr(search, "verdict", lambda held, beat: f"beat={beat}")
    assert run("search", "VRT", "--seeds", "0") == 0
    assert "beat=None" in capsys.readouterr().out


def test_replay_passes_the_real_bar_size(market, monkeypatch):
    seen = {}
    monkeypatch.setattr(replay, "questions", lambda frames, strat, **kw: (seen.update(kw), ([], 0))[1])
    assert run("replay", "--plan") == 0
    assert seen["bar"] == timedelta(minutes=15)


@pytest.mark.parametrize("flag", ["--background", "--restart"])
def test_background_dashboard_wont_pretend_to_use_another_port(capsys, monkeypatch, flag):
    monkeypatch.setattr(launchd, "start_dashboard", lambda *a: pytest.fail("started"))
    monkeypatch.setattr(launchd, "restart_dashboard", lambda *a: pytest.fail("restarted"))
    assert run("dashboard", flag, "--port", "9000") == 2
    assert "always uses port 8787" in capsys.readouterr().err
