"""The MCP server drives the dashboard API in-process: same validation, same refusals."""

import asyncio
import json

import pytest

from taylortrader import mcp_server
from taylortrader.config import Config, StrategyConfig, load_config
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


def call(srv, name, **args):
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
