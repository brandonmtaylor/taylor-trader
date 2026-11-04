"""MCP server: let Claude (or any MCP client) check on and adjust TaylorTrader.

Runs locally over stdio, started by the client (Claude Code, Claude desktop). It drives the same
API the dashboard uses, in-process, so every change goes through the same validation and hard
limits, and is written to the journal as coming from Claude.

What it can't do, on purpose: switch to live trading, or read or change API keys and passwords.
Those stay in the dashboard. Selling needs an exact confirmation value, like the dashboard's.

    claude mcp add taylortrader -- uv --directory /path/to/taylor-trader run taylortrader mcp
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

INSTRUCTIONS = """TaylorTrader is a trading bot running on the user's own Mac against their own Alpaca \
account (usually paper money; check get_status for the mode). Use these tools to report on it and, \
when the user asks, to adjust it.

- Read before you change: get_status and get_strategies show the current setup.
- Changes are validated exactly as in the dashboard. Hard limits can't be exceeded; if a change is \
refused, report the reason rather than working around it.
- The running service picks up strategy and limit changes within a minute and restarts itself.
- Selling places real orders on the account (paper or live). Only sell when the user explicitly asks, \
and pass the confirmation value they would type.
- You can't switch to live trading or touch API keys. If asked, tell the user to use the dashboard.
- This is software, not financial advice. Don't present backtests or AI decisions as predictions."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
SELL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
STRATEGY_FIELDS = ("name", "symbols", "timeframe", "allocation_pct", "stop_loss_pct", "take_profit_pct",
                   "enabled", "params")


class Bot:
    """The dashboard's API, called in-process: no server, no port, same rules."""

    def __init__(self, config_path: Path):
        from fastapi.testclient import TestClient

        from taylortrader.web.api import create_app
        from taylortrader.web.runtime import Runtime

        self.rt = Runtime(config_path=config_path)
        origin = "http://127.0.0.1"
        self.http = TestClient(create_app(self.rt), base_url=origin)
        self.write_headers = {"X-TaylorTrader": "1", "Origin": origin}

    def get(self, path: str) -> Any:
        return self._check(self.http.get(path))

    def send(self, method: str, path: str, body: dict | None = None, note: str = "") -> Any:
        r = self._check(self.http.request(method, path, json=body, headers=self.write_headers))
        if note and self.rt.journal:
            self.rt.journal.event("claude", f"{note} (from Claude via MCP)")
        return r

    @staticmethod
    def _check(r) -> Any:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if isinstance(detail, list):
                detail = "; ".join(d.get("msg", str(d)) for d in detail)
            raise ToolError(str(detail))
        return r.json()

    # --- settings helpers -----------------------------------------------------------------------

    def config(self) -> dict:
        return self.get("/api/settings")["config"]

    def save(self, config: dict, note: str) -> dict:
        return self.send("PUT", "/api/settings", {"config": config}, note)["config"]


def _strategy_view(cfg: dict) -> list[dict]:
    return [{"number": i + 1, **s} for i, s in enumerate(cfg["strategies"])]


def build(config_path: Path = Path("config.toml")) -> MCPServer:
    bot = Bot(config_path)
    mcp = MCPServer("TaylorTrader", instructions=INSTRUCTIONS)

    # --- reading ----------------------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    def get_status() -> dict:
        """Mode (paper, live or dry run), whether the service is running (heartbeat), whether new
        buys are paused, market hours, the strategies, and the safety limits."""
        s = bot.get("/api/status")
        return {k: s[k] for k in ("mode_label", "mode", "dry_run", "heartbeat", "kill_switch", "market",
                                  "strategies", "limits", "alerts", "connected", "broker_error")}

    @mcp.tool(annotations=READ)
    def get_account() -> dict:
        """Equity, cash, today's change, and every position (value, gain, stop-loss, which strategy owns
        it, whether a sell is already waiting)."""
        return bot.get("/api/account")

    @mcp.tool(annotations=READ)
    def get_results() -> dict:
        """Closed-trade results from Alpaca's fills: win rate, average win and loss, realized P&L, streak,
        and a breakdown by strategy. Trades the bot didn't open are reported separately."""
        return bot.get("/api/results")

    @mcp.tool(annotations=READ)
    def get_orders(limit: int = 20) -> list[dict]:
        """Recent orders the bot placed or decided against, newest first, each with its reason."""
        return bot.get(f"/api/orders?limit={max(1, min(limit, 200))}")

    @mcp.tool(annotations=READ)
    def get_events(limit: int = 20) -> list[dict]:
        """The service log, newest first: starts, restarts, errors, pauses, settings changes."""
        return bot.get(f"/api/events?limit={max(1, min(limit, 200))}")

    @mcp.tool(annotations=READ)
    def get_ai_decisions(limit: int = 20) -> list[dict]:
        """What the AI strategy asked its model and why it answered as it did, with pros and cons."""
        return bot.get(f"/api/ai/decisions?limit={max(1, min(limit, 100))}")

    @mcp.tool(annotations=READ)
    def get_strategies() -> dict:
        """The configured strategies (numbered, for update_strategy and remove_strategy), the strategy
        types available with their parameters, the safety limits, and the hard ceilings."""
        s = bot.get("/api/settings")
        return {"strategies": _strategy_view(s["config"]), "available": s["catalog"],
                "limits": s["config"]["guardrails"], "hard_limits": s["hard_limits"],
                "timeframes": s["timeframes"], "ai_model": s["config"]["ai"]}

    @mcp.tool(annotations=READ)
    def run_backtest(strategy: str, symbol: str, timeframe: str = "1Hour", days: int = 365,
                     params: dict | None = None, stop_loss_pct: float = 12, take_profit_pct: float = 0) -> dict:
        """How a strategy would have done on one symbol over past data, compared with buy-and-hold.
        History, not a prediction. The AI strategy can't be backtested."""
        r = bot.send("POST", "/api/backtest", {
            "strategy": strategy, "symbol": symbol.upper(), "timeframe": timeframe, "days": days,
            "params": params or {}, "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct})
        return {"symbol": r["symbol"], "timeframe": r["timeframe"], "summary": r["summary"]}

    # --- changing strategies and limits ---------------------------------------------------------------

    @mcp.tool(annotations=CHANGE)
    def update_strategy(number: int, symbols: list[str] | None = None, timeframe: str | None = None,
                        allocation_pct: float | None = None, stop_loss_pct: float | None = None,
                        take_profit_pct: float | None = None, enabled: bool | None = None,
                        params: dict | None = None) -> dict:
        """Change one strategy (by its number from get_strategies). Only the fields you pass change;
        `params` are merged into the existing ones. Validated like the dashboard."""
        cfg = bot.config()
        if not 1 <= number <= len(cfg["strategies"]):
            raise ToolError(f"There's no strategy {number}; there are {len(cfg['strategies'])}.")
        s = cfg["strategies"][number - 1]
        changes = {k: v for k, v in {"symbols": [x.upper() for x in symbols] if symbols is not None else None,
                                     "timeframe": timeframe, "allocation_pct": allocation_pct,
                                     "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
                                     "enabled": enabled}.items() if v is not None}
        s.update(changes)
        if params:
            s["params"] = {**s.get("params", {}), **params}
            changes["params"] = params
        if not changes:
            raise ToolError("Nothing to change: pass at least one field.")
        saved = bot.save(cfg, f"strategy {number} ({s['name']}) changed: {changes}")
        return {"saved": True, "strategies": _strategy_view(saved),
                "note": "The service switches to these within about a minute."}

    @mcp.tool(annotations=CHANGE)
    def add_strategy(name: str, symbols: list[str], timeframe: str = "1Hour", allocation_pct: float = 10,
                     stop_loss_pct: float = 10, take_profit_pct: float = 0, params: dict | None = None,
                     enabled: bool = True) -> dict:
        """Add a strategy. `name` is one of the available types from get_strategies. Each symbol can
        belong to only one strategy."""
        cfg = bot.config()
        cfg["strategies"].append({"name": name, "symbols": [x.upper() for x in symbols], "timeframe": timeframe,
                                  "allocation_pct": allocation_pct, "stop_loss_pct": stop_loss_pct,
                                  "take_profit_pct": take_profit_pct, "enabled": enabled, "params": params or {}})
        saved = bot.save(cfg, f"added strategy {name} on {', '.join(symbols)}")
        return {"saved": True, "strategies": _strategy_view(saved)}

    @mcp.tool(annotations=CHANGE)
    def remove_strategy(number: int) -> dict:
        """Remove a strategy (by number). Positions it holds stay open with their stop-losses, but no
        strategy will manage them until you assign the symbol again."""
        cfg = bot.config()
        if not 1 <= number <= len(cfg["strategies"]):
            raise ToolError(f"There's no strategy {number}; there are {len(cfg['strategies'])}.")
        gone = cfg["strategies"].pop(number - 1)
        saved = bot.save(cfg, f"removed strategy {number} ({gone['name']} on {', '.join(gone['symbols'])})")
        return {"saved": True, "removed": gone, "strategies": _strategy_view(saved)}

    @mcp.tool(annotations=CHANGE)
    def update_limits(max_position_pct: float | None = None, max_total_exposure_pct: float | None = None,
                      max_daily_loss_pct: float | None = None, max_trades_per_day: int | None = None,
                      max_stop_loss_pct: float | None = None) -> dict:
        """Change the account-wide safety limits. They can't go past the hard ceilings in get_strategies."""
        cfg = bot.config()
        changes = {k: v for k, v in locals().items() if k not in ("cfg",) and v is not None and k.startswith("max_")}
        if not changes:
            raise ToolError("Nothing to change: pass at least one limit.")
        cfg["guardrails"].update(changes)
        saved = bot.save(cfg, f"limits changed: {changes}")
        return {"saved": True, "limits": saved["guardrails"]}

    # --- controls ---------------------------------------------------------------------------------------

    @mcp.tool(annotations=CHANGE)
    def pause_new_buys(paused: bool = True) -> dict:
        """Pause (or resume, with paused=false) new buys. Sells and stop-losses keep working while paused."""
        s = bot.send("POST", "/api/pause", {"paused": paused}, "new buys paused" if paused else "new buys resumed")
        return {"new_buys_paused": s["kill_switch"]}

    @mcp.tool(annotations=CHANGE)
    def set_mode(mode: Literal["dry-run", "paper"]) -> dict:
        """Switch between dry run (decides and alerts, places no orders) and paper trading. Going live
        isn't available here: it needs the dashboard."""
        s = bot.send("POST", "/api/mode", {"mode": mode}, f"mode set to {mode}")
        return {"mode": s["mode_label"]}

    @mcp.tool(annotations=SELL)
    def sell_position(symbol: str, confirm: str) -> dict:
        """Sell all of one position at market (waits for the open if the market is closed). Places a
        real order. `confirm` must equal the symbol, and should come from the user."""
        symbol = symbol.strip().upper()
        if confirm.strip().upper() != symbol:
            raise ToolError(f"To sell, confirm must be exactly {symbol}.")
        return bot.send("POST", "/api/positions/sell", {"symbol": symbol}, f"sold {symbol}")

    @mcp.tool(annotations=SELL)
    def sell_everything(confirm: str) -> dict:
        """Pause new buys, cancel open orders and sell every position in the account, including ones the
        bot didn't buy. Places real orders. `confirm` must be exactly SELL ALL, from the user."""
        return bot.send("POST", "/api/positions/sell-all", {"confirm": confirm}, "sell everything")

    return mcp


def main(config_path: Path = Path("config.toml")) -> None:
    import logging

    # stdout is the MCP channel: anything else printed there corrupts it, so logs go to stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    build(config_path).run("stdio")
