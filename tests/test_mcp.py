"""The MCP server drives the dashboard API in-process: same validation, same refusals."""

import asyncio
import dataclasses
import json

import pytest

from taylortrader import mcp_server
from taylortrader.config import Config, StrategyConfig, load_config, save_config
from taylortrader.service import kill_file
from tests.test_web import make_client, no_keychain  # noqa: F401  (fixture used automatically)


@pytest.fixture
def server(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ALPACA_PAPER_KEY_ID=goodpaper\nALPACA_PAPER_SECRET_KEY=s3cretpaper\n")
    cfg = Config(data_dir=tmp_path / "var", strategies=[StrategyConfig(name="macd_rsi", symbols=["VRT"])])
    client = make_client(tmp_path, cfg)  # writes config.toml and a fake broker into a Runtime
    monkeypatch.setattr("taylortrader.web.runtime.Runtime", lambda config_path: client.rt)
    srv = mcp_server.build(client.path)
    srv.client = client
    return srv


def call(srv, name, /, **args):
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        r = asyncio.run(srv.call_tool(name, args))
    except ToolError as e:  # what the client would see as an error result
        raise RuntimeError(str(e)) from None
    text = "".join(getattr(c, "text", "") for c in r.content)
    if r.is_error:
        raise RuntimeError(text)
    return r.structured_content.get("result", r.structured_content) if r.structured_content else json.loads(text)


def test_reads(server):
    assert call(server, "get_status")["mode"] == "paper"
    assert {p["symbol"] for p in call(server, "get_account")["positions"]} == {"VRT", "AAPL"}
    assert call(server, "get_strategies")["strategies"][0]["name"] == "macd_rsi"


def test_change_strategy_is_validated_saved_and_journaled(server):
    out = call(server, "update_strategy", number=1, symbols=["vrt", "nvda"], params={"rsi_buy": 33})
    assert out["strategies"][0]["symbols"] == ["VRT", "NVDA"]
    cfg = load_config(server.client.path)
    assert cfg.strategies[0].symbols == ["VRT", "NVDA"] and cfg.strategies[0].params["rsi_buy"] == 33
    assert any("from Claude via MCP" in e["message"] for e in call(server, "get_events"))
    with pytest.raises(RuntimeError, match="hard limit"):
        call(server, "update_limits", max_daily_loss_pct=50)
    with pytest.raises(RuntimeError, match="no strategy 7"):
        call(server, "update_strategy", number=7, enabled=False)


def test_pause_and_mode_but_never_live(server):
    assert call(server, "pause_new_buys")["new_buys_paused"] is True
    assert kill_file(server.client.rt.settings).exists()
    assert "dry run" in call(server, "set_mode", mode="dry-run")["mode"]
    with pytest.raises(RuntimeError):
        call(server, "set_mode", mode="live")
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert not any("secret" in n or "key" in n or "live" in n for n in names)


def test_selling_needs_the_exact_confirmation(server):
    with pytest.raises(RuntimeError, match="confirm must be exactly AAPL"):
        call(server, "sell_position", symbol="AAPL", confirm="yes")
    assert server.client.broker.closes == []
    call(server, "sell_position", symbol="aapl", confirm="AAPL")
    assert server.client.broker.closes == ["AAPL"]
    with pytest.raises(RuntimeError, match="SELL ALL"):
        call(server, "sell_everything", confirm="sell all please")


def test_invest_plan_can_be_read_and_turned_down_but_not_up(server):
    from dataclasses import replace

    from taylortrader.config import InvestConfig, save_config

    c = server.client
    c.broker.asset_status = lambda s: (True, True)
    cfg = replace(load_config(c.path), invest=InvestConfig(
        enabled=True, preview=False, amount=200, holdings=[{"symbol": "VTI", "weight": 100.0}]))
    save_config(cfg, c.path)
    c.rt.reload()
    plan = call(server, "get_invest_plan")
    assert plan["plan"]["amount"] == 200 and plan["status"]["next"]["split"] == {"VTI": 200.0}
    assert call(server, "set_invest_plan_state", state="preview") == {"enabled": True, "preview": True}
    assert call(server, "set_invest_plan_state", state="off") == {"enabled": False, "preview": True}
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert "set_invest_plan_state" in tools and not any("start" in t and "invest" in t for t in tools)


def go_live(server):
    c = server.client
    with (c.path.parent / ".env").open("a") as f:
        f.write("ALPACA_LIVE_KEY_ID=goodlive\nALPACA_LIVE_SECRET_KEY=s3cretlive\nTAYLORTRADER_ALLOW_LIVE=1\n")
    save_config(dataclasses.replace(load_config(c.path), mode="live"), c.path)
    c.rt.reload()
    return c


def test_claude_cant_loosen_limits_on_a_live_account(server):
    go_live(server)
    with pytest.raises(RuntimeError, match="real money"):
        call(server, "update_limits", max_daily_loss_pct=5)
    call(server, "update_limits", max_daily_loss_pct=1)  # tightening still works


def test_claude_can_edit_a_strategy_on_a_live_account(server):
    c = go_live(server)
    call(server, "update_strategy", number=1, allocation_pct=20)
    assert load_config(c.path).strategies[0].allocation_pct == 20
    with pytest.raises(RuntimeError):  # past the stop-loss limit: refused, same as the dashboard
        call(server, "update_strategy", number=1, stop_loss_pct=99)


def test_dropping_a_held_symbol_on_live_needs_confirming(server):
    c = go_live(server)
    with pytest.raises(RuntimeError, match="KEEP OPEN"):
        call(server, "remove_strategy", number=1)
    with pytest.raises(RuntimeError, match="KEEP OPEN"):
        call(server, "update_strategy", number=1, symbols=["NVDA"])
    assert load_config(c.path).strategies[0].symbols == ["VRT"]
    call(server, "remove_strategy", number=1, confirm="KEEP OPEN")
    assert load_config(c.path).strategies == []


def test_removing_a_strategy_with_nothing_held_needs_no_confirming(server):
    c = go_live(server)
    call(server, "add_strategy", name="rule", symbols=["NVDA"])
    call(server, "remove_strategy", number=2)
    assert len(load_config(c.path).strategies) == 1


def test_a_described_strategy_can_be_added_as_rules(server):
    words = call(server, "get_strategies")["rule_language"]
    assert "rsi" in words["names"] and "ma_200" in words["names"]
    rules = {"buy_when": "prev_rsi < 30 and rsi >= 30", "sell_when": "rsi > 70"}
    out = call(server, "add_strategy", name="rule", symbols=["aapl"], timeframe="1Day", params=rules)
    assert out["strategies"][-1]["params"] == rules
    assert load_config(server.client.path).strategies[-1].symbols == ["AAPL"]


def test_a_rule_it_cant_read_is_refused_and_not_saved(server):
    with pytest.raises(RuntimeError, match="unknown name"):
        call(server, "add_strategy", name="rule", symbols=["AAPL"], params={"buy_when": "earnings > 0"})
    assert len(load_config(server.client.path).strategies) == 1


def test_claude_can_add_a_strategy_on_a_live_account_within_the_limits(server):
    c = go_live(server)
    call(server, "add_strategy", name="rule", symbols=["AAPL"], allocation_pct=10, stop_loss_pct=8)
    assert load_config(c.path).strategies[-1].symbols == ["AAPL"]
    with pytest.raises(RuntimeError):  # past the stop-loss limit: refused, same as the dashboard
        call(server, "add_strategy", name="rule", symbols=["MSFT"], stop_loss_pct=99)
    assert len(load_config(c.path).strategies) == 2


def test_an_idea_gets_the_honest_test(server, monkeypatch):
    from taylortrader import search
    from tests.test_search import _bars

    monkeypatch.setattr(search, "load_frames", lambda md, symbols, tf, days, say: {"AAA": _bars(900), "BBB": _bars(900)})
    out = call(server, "test_strategy_idea", buy_when="rsi < 45", sell_when="rsi > 55")
    assert out["verdict"] and out["holdout"]["trades"] > 0 and out["luck"]["runs"] == 100
    assert "symbols" not in out["older_data"] and out["stocks"] == 2


def test_an_idea_it_cant_read_is_refused(server):
    with pytest.raises(RuntimeError, match="unknown name"):
        call(server, "test_strategy_idea", buy_when="earnings > 0")
