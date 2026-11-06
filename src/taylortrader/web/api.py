"""Dashboard API. Listens on 127.0.0.1 only, unless Wi-Fi access is on (see web/remote.py).

Reads are GETs. Every change (settings, keys, mode, pause, service) is a POST/PUT/DELETE that
must carry the `X-TaylorTrader: 1` header and an Origin of this same address:
- a custom header forces a CORS preflight, which this server never approves, so another
  website open in your browser can't send one;
- the Origin check refuses anything that did get through from elsewhere;
- other Host names are refused (DNS rebinding);
- with Wi-Fi access on, devices other than this Mac must sign in with the passcode first.
Secret values go in and never come back out: the API reports only whether, and where, each is set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from guardrails import find_unmanaged_positions
from guardrails.limits import HARD_LIMITS
from taylortrader import launchd
from taylortrader.config import (
    FEEDS,
    TIMEFRAMES,
    Config,
    ConfigError,
    config_to_dict,
    parse_config,
    parse_invest,
    save_config,
)
from taylortrader.invest import NY
from taylortrader.secrets import (
    LIVE_CONFIRM_VAR,
    SETTABLE,
    AlpacaCredentials,
    SecretsError,
    delete_secret,
    get_secret,
    keychain_available,
    secret_source,
    set_secret,
)
from taylortrader.service import describe_mode, kill_file
from taylortrader.strategies import list_strategies
from taylortrader.web.remote import COOKIE, SESSION_DAYS, Sessions, host_allowed, is_loopback, mac_name
from taylortrader.web.runtime import Runtime

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
BENCHMARK = "SPY"
GO_LIVE_PHRASE = "GO LIVE"
CASH_YIELD = 4.0  # % a year on uninvested cash in backtests, so time out of the market isn't scored as 0%
SELL_ALL_PHRASE = "SELL ALL"
WRITE_HEADER = "x-taylortrader"
FORBIDDEN = "Changes must come from the TaylorTrader dashboard."


class TTLCache:
    """Tiny cache so a browser polling every few seconds doesn't hammer Alpaca's rate limit."""

    MAX_ENTRIES = 256  # keys can come from the request: don't let them grow without end

    def __init__(self, ttl: float):
        self.ttl, self._data = ttl, {}

    def get(self, key: str, fn):
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = fn()
        if len(self._data) >= self.MAX_ENTRIES:
            self._data.clear()
        self._data[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        self._data.clear()


def _broker_call(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001  any broker failure becomes a clear 503
        log.warning("broker call failed: %s", e)
        raise HTTPException(503, f"Can't reach Alpaca: {type(e).__name__}: {e}") from None


def exit_fields(lv: dict, price: float, qty: float) -> dict:
    """Where a position's stop-loss and take-profit sit, as prices and as distances from now."""
    stop, target = lv.get("stop"), lv.get("target")
    return {
        "stop_price": stop,
        "stop_pct": (stop / price - 1) * 100 if stop and price else None,
        "stop_loss_usd": (stop - price) * qty if stop and price else None,
        "target_price": target,
        "target_pct": (target / price - 1) * 100 if target and price else None,
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _downsample(series, n: int = 400) -> list[dict]:
    idx = list(range(0, len(series), max(1, len(series) // n)))
    if idx and idx[-1] != len(series) - 1:
        idx.append(len(series) - 1)
    return [{"t": int(series.index[i].timestamp()), "v": round(float(series.iloc[i]), 4)} for i in idx]


# --- request bodies ------------------------------------------------------------------------------

class SettingsBody(BaseModel):
    config: dict[str, Any]
    revision: str | None = None  # the settings this edit started from; a stale page is refused


def config_revision(cfg: Config) -> str:
    return hashlib.sha256(json.dumps(config_to_dict(cfg), sort_keys=True, default=str).encode()).hexdigest()[:16]


SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}(/[A-Z]{2,5})?$")  # stocks, and crypto pairs like BTC/USD


def clean_symbol(symbol: str) -> str:
    """Symbols become part of cache file names and broker requests: only ticker shapes get through."""
    symbol = symbol.strip().upper()
    if not SYMBOL.match(symbol):
        raise HTTPException(422, f"{symbol[:15]!r} doesn't look like a ticker symbol.")
    return symbol


class InvestBody(BaseModel):
    plan: dict[str, Any]
    confirm: str | None = Field(default=None, max_length=80)


EVERY_LABEL = {"week": "WEEKLY", "2weeks": "EVERY 2 WEEKS", "month": "MONTHLY", "once": "ONCE"}


def invest_confirm_phrase(inv, mode: str) -> str:
    """What the user types to let the plan spend money: the amount and schedule, so it's read, not clicked."""
    phrase = f"BUY ${inv.amount:,.2f} {EVERY_LABEL[inv.every]}"
    return f"LIVE {phrase}" if mode == "live" else phrase


class SecretBody(BaseModel):
    name: str
    value: SecretStr


class AlpacaTestBody(BaseModel):
    mode: Literal["paper", "live"]
    key_id: SecretStr | None = None      # omitted: test the stored keys
    secret_key: SecretStr | None = None


class ModeBody(BaseModel):
    mode: Literal["dry-run", "paper", "live"]
    confirm: str = ""


class LoginBody(BaseModel):
    passcode: SecretStr = Field(max_length=64)


class PauseBody(BaseModel):
    paused: bool


class SellBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)


class SellAllBody(BaseModel):
    confirm: str = ""


class ServiceBody(BaseModel):
    action: Literal["start", "stop", "restart"]


class BacktestBody(BaseModel):
    strategy: str
    symbol: str = Field(min_length=1, max_length=15)
    timeframe: str = "5Min"
    params: dict[str, Any] = {}
    days: int = Field(365, ge=5, le=1100)
    allocation_pct: float = Field(100, gt=0, le=100)
    stop_loss_pct: float = Field(12, gt=0, le=HARD_LIMITS.max_stop_loss_pct)
    take_profit_pct: float = Field(0, ge=0)


SIGN_IN = "Sign in with the dashboard passcode."
OPEN_PATHS = {"/api/session", "/api/login"}
MAC_ONLY = "Going live and API keys can only be changed on the Mac running TaylorTrader."


def create_app(rt: Runtime, cache_ttl: float = 10.0, project: Path | None = None,
               passcode: str | None = None) -> FastAPI:
    """`project` is the home folder (default: the current one; the command line changes into it).
    `passcode` set means Wi-Fi access is on: other devices may connect, after signing in."""
    project = project or Path.cwd()
    app = FastAPI(title="TaylorTrader", docs_url=None, redoc_url=None, openapi_url=None)
    sessions = Sessions(passcode) if passcode else None
    name = mac_name() if passcode else None

    def here(request: Request) -> bool:
        """This Mac itself. Always true with Wi-Fi access off (the server only listens locally)."""
        return sessions is None or is_loopback(request.client.host if request.client else None)

    def signed_in(request: Request) -> bool:
        return here(request) or (sessions is not None and sessions.valid(request.cookies.get(COOKIE)))

    @app.middleware("http")
    async def gate(request: Request, call_next):
        # Reject Host names that aren't this machine (blocks DNS rebinding from web pages).
        if not host_allowed(request.headers.get("host", ""), sessions is not None, name):
            return PlainTextResponse("Invalid host header", status_code=400)
        path = request.url.path
        # Browsers say where a request came from. Another website must not be able to trigger reads:
        # they cost Keychain lookups and Alpaca's rate limit, even if the site can't see the answer.
        if path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": FORBIDDEN}, status_code=403)
        if path.startswith("/api/") and path not in OPEN_PATHS and not signed_in(request):
            return JSONResponse({"detail": SIGN_IN}, status_code=401)
        return await call_next(request)

    def mac_only(request: Request) -> None:
        """Same line the phone app will hold: real money and keys are changed on the Mac."""
        if not here(request):
            raise HTTPException(403, MAC_ONLY)

    def check_remote_settings(request: Request, current: Config, new: Config) -> None:
        """What a device on the Wi-Fi can't change. Where alerts and AI requests go decides where the
        email password and API keys are sent, so a stolen session could collect them there. And with
        real money, settings are changed on the Mac: a phone can still pause and sell."""
        if here(request):
            return
        if current.mode == "live":
            raise HTTPException(403, "With real money, settings can only be changed on the Mac.")
        destinations = lambda c: (c.alerts, c.ai.provider, c.ai.endpoint)
        if destinations(current) != destinations(new):
            raise HTTPException(403, "Alert destinations and the AI provider can only be changed on the Mac.")

    cache = TTLCache(cache_ttl)
    slow_cache = TTLCache(max(cache_ttl, 300.0))  # price history and grades: minutes old is fine
    dev_origins = {"http://localhost:5173", "http://127.0.0.1:5173"} if os.environ.get("TAYLORTRADER_DEV") else set()

    def guard(request: Request) -> None:
        """Every write must come from this dashboard, not another site open in the browser."""
        origin = request.headers.get("origin")
        same = f"http://{request.headers.get('host', '')}"
        if request.headers.get(WRITE_HEADER) != "1" or origin not in {same, *dev_origins}:
            raise HTTPException(403, FORBIDDEN)

    def need_broker():
        if rt.broker is None:
            raise HTTPException(503, rt.broker_error or "Not connected to Alpaca.")
        return rt.broker

    def changed() -> None:
        rt.reload()
        cache.clear()

    def save(new: Config) -> Config:
        try:
            cfg = save_config(new, rt.config_path)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        changed()
        return cfg

    def owners() -> dict[str, str]:
        return {s: sc.name for sc in rt.settings.enabled_strategies for s in sc.symbols}

    def test_keys(mode: str, key_id: SecretStr | None = None, secret: SecretStr | None = None) -> dict[str, Any]:
        from taylortrader.broker import AlpacaBroker

        prefix = "ALPACA_PAPER" if mode == "paper" else "ALPACA_LIVE"
        key_id = key_id or get_secret(f"{prefix}_KEY_ID", rt.dotenv_path)
        secret = secret or get_secret(f"{prefix}_SECRET_KEY", rt.dotenv_path)
        if not key_id or not secret:
            raise HTTPException(422, f"No {mode} keys saved yet.")
        try:
            info = AlpacaBroker(AlpacaCredentials(key_id, secret, paper=mode == "paper")).account_info()
        except Exception as e:  # noqa: BLE001 - reported to the user; Alpaca errors never contain keys
            text = str(e).lower()
            if any(w in text for w in ("unauthorized", "not authorized", "forbidden", "40110000")):
                raise HTTPException(422, f"Alpaca didn't accept these as {mode} keys. Check they're from your "
                                         f"{mode} account and copied in full.") from None
            raise HTTPException(503, f"Couldn't reach Alpaca: {type(e).__name__}: {str(e)[:200]}") from None
        return {"ok": True, **info}

    # --- reads -----------------------------------------------------------------------------------

    @app.get("/api/setup")
    def setup() -> dict[str, Any]:
        """What the first-run screens need. Secrets map to where they're stored, never to values."""
        cfg = rt.settings
        return {
            "configured": rt.configured,
            "config_path": str(rt.config_path),
            "config_error": rt.config_error,
            "connected": rt.broker is not None,
            "broker_error": rt.broker_error,
            "mode": cfg.mode,
            "dry_run": cfg.dry_run,
            "has_strategy": bool(cfg.enabled_strategies),
            "has_plan": cfg.invest.enabled and bool(cfg.invest.holdings),
            "secret_store": "Keychain" if keychain_available() else ".env",
            "secrets": {n: secret_source(n, rt.dotenv_path) for n in SETTABLE},
            "service": launchd.status(),
        }

    @app.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        return {"wifi": sessions is not None, "here": here(request), "signed_in": signed_in(request)}

    @app.post("/api/login", dependencies=[Depends(guard)])
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if sessions is None or here(request):
            return session(request)
        device = request.client.host if request.client else "?"
        wait = sessions.wait_s(device)
        if wait:
            raise HTTPException(429, f"Too many wrong passcodes. Try again in {max(1, round(wait / 60))} min.")
        if not sessions.check(device, body.passcode.get_secret_value()):
            log.warning("wrong dashboard passcode from %s", device)
            raise HTTPException(401, "That passcode isn't right. It's shown by `taylortrader wifi on` on the Mac.")
        log.info("dashboard signed in from %s", device)
        rt.journal.event("settings", f"dashboard signed in from {device}")
        response.set_cookie(COOKIE, sessions.issue(), max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", path="/")
        return {"wifi": True, "here": False, "signed_in": True}

    @app.post("/api/logout", dependencies=[Depends(guard)])
    def logout(request: Request, response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE, path="/")
        return {"wifi": sessions is not None, "here": here(request), "signed_in": here(request)}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        cfg = rt.settings
        beat = rt.journal.last_beat()
        now = datetime.now(UTC)
        age = (now - beat[0]).total_seconds() if beat else None
        market = None
        if rt.broker is not None:
            try:
                clock = cache.get("clock", lambda: _broker_call(rt.broker.clock))
                market = {"is_open": clock.is_open, "next_open": _iso(clock.next_open),
                          "next_close": _iso(clock.next_close)}
            except HTTPException:
                market = None
        return {
            "configured": rt.configured,
            "connected": rt.broker is not None,
            "broker_error": rt.broker_error,
            "mode": cfg.mode,
            "dry_run": cfg.dry_run,
            "mode_label": describe_mode(cfg.mode, cfg.dry_run),
            "running_mode": beat[1] if beat else None,  # what the service last reported: "ok" or "dry-run"
            "heartbeat": {
                "ts": _iso(beat[0]) if beat else None,
                "age_s": age,
                "stale": age is None or age > cfg.service.heartbeat_stale_minutes * 60,
                "stale_after_s": cfg.service.heartbeat_stale_minutes * 60,
            },
            "kill_switch": kill_file(cfg).exists(),
            "market": market,
            "strategies": [asdict(sc) for sc in cfg.strategies],
            "limits": asdict(cfg.limits),
            "alerts": rt.alert_notes,
            "server_time": now.isoformat(),
        }

    @app.get("/api/account")
    def account() -> dict[str, Any]:
        broker = need_broker()

        def load():
            snap = _broker_call(broker.snapshot)
            levels = _broker_call(broker.exit_levels)
            selling = _broker_call(broker.symbols_selling)
            return snap, levels, selling

        snap, levels, selling = cache.get("account", load)
        stops = {s for s, lv in levels.items() if lv["stop"] is not None}
        own = owners()
        plan = set(rt.settings.invest.symbols)  # the invest plan's holdings: managed, no stop on purpose
        problems = find_unmanaged_positions({s: p for s, p in snap.positions.items() if s not in plan}, stops, own)
        own = {**own, **{s: "invest plan" for s in plan}}
        positions = []
        for p in sorted(snap.positions.values(), key=lambda p: -abs(p.market_value)):
            price = p.market_value / p.qty if p.qty else 0.0
            positions.append({
                "symbol": p.symbol, "qty": p.qty, "market_value": p.market_value,
                "pct_of_equity": p.market_value / snap.equity * 100 if snap.equity else 0,
                "avg_entry_price": p.avg_entry_price, "price": price,
                "unrealized_pct": (price / p.avg_entry_price - 1) * 100 if p.avg_entry_price else None,
                "has_stop": p.symbol in stops, "owner": own.get(p.symbol), "invest": p.symbol in plan,
                **exit_fields(levels.get(p.symbol, {}), price, p.qty),
                "selling": p.symbol in selling,
                "issues": problems.get(p.symbol, []),
            })
        at_risk = [x["stop_loss_usd"] for x in positions if x["stop_loss_usd"] is not None and not x["selling"]]
        return {
            # What the account loses from here if every stop-loss fires (positions with a known stop).
            "stops_at_risk_usd": sum(at_risk) if at_risk else None,
            "equity": snap.equity, "cash": snap.cash, "day_start_equity": snap.day_start_equity,
            "day_pnl_pct": (snap.equity / snap.day_start_equity - 1) * 100 if snap.day_start_equity else 0,
            "trades_today": snap.trades_today,
            "exposure_pct": snap.exposure / snap.equity * 100 if snap.equity else 0,
            "positions": positions,
        }

    @app.get("/api/performance")
    def performance(period: str = Query("1M", pattern="^(1M|3M|1A)$")) -> dict[str, Any]:
        broker = need_broker()

        def load():
            equity = _broker_call(lambda: broker.portfolio_history(period))
            bench: list[tuple[int, float]] = []
            if equity and rt.market_data is not None:
                start = datetime.fromtimestamp(equity[0][0], UTC) - timedelta(days=5)
                try:
                    df = rt.market_data.bars(BENCHMARK, "1Day", start)
                    by_day = {ts.date(): float(c) for ts, c in df["close"].items()}
                    for t, _ in equity:
                        day = datetime.fromtimestamp(t, UTC).date()
                        close = by_day.get(day) or next(
                            (by_day[d] for d in sorted(by_day, reverse=True) if d <= day), None)
                        if close:
                            bench.append((t, close))
                except Exception as e:  # noqa: BLE001  the benchmark is optional
                    log.warning("benchmark unavailable: %s", e)
            return {"period": period, "benchmark": BENCHMARK,
                    "equity": [{"t": t, "v": v} for t, v in equity],
                    "benchmark_series": [{"t": t, "v": v} for t, v in bench]}

        return cache.get(f"perf:{period}", load)

    @app.get("/api/orders")
    def orders(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return [dict(r) for r in rt.journal.recent_orders(limit)]

    @app.get("/api/events")
    def events(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        rows = rt.journal.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @app.get("/api/history")
    def history() -> dict[str, Any]:
        rows = [dict(r) for r in rt.journal.db.execute(
            "SELECT * FROM legacy_trades WHERE sell_date IS NOT NULL ORDER BY sell_date")]
        by_year: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "profit": 0.0})
        for r in rows:
            y = by_year[(r["sell_date"] or "")[:4]]
            y["trades"] += 1
            y["wins"] += 1 if (r["profit"] or 0) > 0 else 0
            y["profit"] += r["profit"] or 0
        return {
            "closed_trades": len(rows),
            "realized": sum(r["profit"] or 0 for r in rows),
            "by_year": [{"year": k, **v} for k, v in sorted(by_year.items())],
            "recent": rows[-25:][::-1],
        }

    @app.get("/api/ai/decisions")
    def ai_decisions(limit: int = Query(30, ge=1, le=200)) -> list[dict]:
        return rt.journal.recent_ai(limit)

    def fills_and_owners():
        broker = need_broker()
        fills = cache.get("fills", lambda: _broker_call(broker.fills))
        strategy_of = {r["broker_order_id"]: r["strategy"] for r in rt.journal.db.execute(
            "SELECT broker_order_id, strategy FROM orders WHERE side='buy' AND broker_order_id IS NOT NULL")}
        return fills, strategy_of

    def need_data():
        if rt.market_data is None:
            raise HTTPException(503, rt.broker_error or "Price data isn't connected.")
        return rt.market_data

    def all_ai(days: int = 120) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return [dict(r) for r in rt.journal.db.execute(
            "SELECT id, ts, strategy, symbol, question, action, confidence, acted, summary, error, features"
            " FROM ai_decisions WHERE ts >= ? ORDER BY ts", (since,))]

    @app.get("/api/results")
    def results() -> dict[str, Any]:
        """Closed trades from Alpaca's fills, credited to the strategy whose buy opened them."""
        from taylortrader.stats import results as compute

        def load():
            fills, strategy_of = fills_and_owners()
            return compute(fills, strategy_of, datetime.now(UTC))

        return cache.get("results", load)

    @app.get("/api/ai/grades")
    def ai_grades() -> dict[str, Any]:
        """Each AI decision scored against what the price did over the next trading day."""
        from taylortrader import grading

        def load():
            decisions = all_ai()
            md = need_data()
            now = datetime.now(UTC)
            bars = {}
            for symbol, start in grading.bars_needed(decisions, now).items():
                try:
                    bars[symbol] = md.bars(symbol, "1Hour", start)
                except Exception as e:  # noqa: BLE001 - that symbol stays "pending"; the rest still grade
                    log.warning("grading %s: no bars: %s", symbol, e)
            return grading.report(grading.grade(decisions, bars))

        return slow_cache.get("ai_grades", load)

    @app.get("/api/chart")
    def trade_chart(symbol: str = Query("", max_length=15),
                    period: str = Query("1M", pattern="^(1W|1M|3M|1A)$")) -> dict[str, Any]:
        """A stock's price with the bot's buys, sells and AI calls on it, and how each trade was timed."""
        if symbol.strip():
            symbol = clean_symbol(symbol)
        from taylortrader import chart

        broker = need_broker()
        fills, strategy_of = fills_and_owners()
        held = cache.get("snapshot", lambda: _broker_call(broker.snapshot)).positions
        # Stocks worth picking: held now, traded this year, or watched by a strategy.
        recent = [f.symbol for f in sorted(fills, key=lambda f: f.ts, reverse=True)]
        watched = [s for sc in rt.settings.enabled_strategies for s in sc.symbols]
        symbols = [s for s in dict.fromkeys([*held, *recent, *watched]) if "/" not in s]  # stocks, not crypto
        symbol = symbol or (symbols[0] if symbols else "")
        if not symbol:
            return {"symbols": [], "symbol": None}
        md = need_data()
        days, timeframe = chart.PERIODS[period]
        now = datetime.now(UTC)

        def load():
            from taylortrader.indicators import add_all

            back = days + chart.WARMUP_DAYS[timeframe]
            df = _broker_call(lambda: md.bars(symbol, timeframe, now - timedelta(days=back)))
            return add_all(df) if not df.empty else df

        df = slow_cache.get(f"bars:{symbol}:{period}", load)
        levels = cache.get("levels", lambda: _broker_call(broker.exit_levels)).get(symbol, {})
        pos = held.get(symbol)
        out = chart.build(symbol, period, df, fills, strategy_of, all_ai(days + 1), levels.get("stop"),
                          pos.qty if pos else 0.0, now, target=levels.get("target"))
        return {"symbols": symbols, **out}

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return {
            "config": config_to_dict(rt.settings),
            "revision": config_revision(rt.settings),
            "configured": rt.configured,
            "catalog": list_strategies(),
            "hard_limits": asdict(HARD_LIMITS),
            "timeframes": list(TIMEFRAMES),
            "feeds": list(FEEDS),
        }

    # --- writes ----------------------------------------------------------------------------------

    @app.put("/api/settings", dependencies=[Depends(guard)])
    def put_settings(body: SettingsBody, request: Request) -> dict[str, Any]:
        """Strategies, limits, alerts, data feed. Mode is never changed here: see /api/mode."""
        current = rt.settings
        if body.revision and body.revision != config_revision(current):
            raise HTTPException(409, "Settings changed somewhere else since this page loaded (another tab, "
                                     "Claude or a device). Reload the page and make your change again.")
        raw = dict(body.config)
        raw.update(mode=current.mode, dry_run=current.dry_run, data_dir=str(current.data_dir))
        raw["invest"] = asdict(current.invest)  # the invest plan has its own endpoint, with its own confirmation
        try:
            new = parse_config(raw)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        check_remote_settings(request, current, new)
        save(new)
        rt.journal.event("settings", "settings saved from the dashboard")
        return settings()

    # --- invest plan ---------------------------------------------------------------------------------

    def invest_runner():
        from taylortrader.invest import InvestRunner

        broker = need_broker()
        return InvestRunner(rt.settings, broker, rt.journal, None, None, lambda: datetime.now(UTC),
                            dry_run=rt.settings.dry_run)

    @app.get("/api/invest")
    def invest_status() -> dict[str, Any]:
        from guardrails.invest import HARD_MAX_CONTRIBUTION
        from taylortrader.invest import EXAMPLES

        inv = rt.settings.invest
        out: dict[str, Any] = {"plan": asdict(inv), "examples": EXAMPLES, "hard_max": HARD_MAX_CONTRIBUTION,
                               "account_mode": "dry-run" if rt.settings.dry_run else rt.settings.mode,
                               "history": invest_history(), "status": None, "cash": None, "error": None}
        try:
            runner = invest_runner()
            snap = cache.get("snapshot", lambda: _broker_call(runner.broker.snapshot))
            out["status"], out["cash"] = runner.status(snap), snap.cash
        except HTTPException as e:
            out["error"] = e.detail
        return out

    def invest_history() -> list[dict[str, Any]]:
        return rt.journal.invest_history() if rt.journal else []

    @app.get("/api/invest/whatif")
    def invest_whatif(years: int = Query(10, ge=1, le=12)) -> dict[str, Any]:
        """Your plan's amount and schedule, run on the past with your mix and each example mix."""
        from taylortrader import whatif
        from taylortrader.config import InvestConfig
        from taylortrader.invest import EXAMPLES

        data = need_data()
        inv = rt.settings.invest
        amount = inv.amount if inv.amount >= 1 else 200.0
        every, day = (inv.every, inv.day) if inv.amount >= 1 else ("month", 1)
        mixes = ([{"id": "yours", "name": "Your mix", "holdings": inv.holdings}] if inv.holdings else []) + [
            {"id": e["id"], "name": e["name"], "holdings": e["holdings"]} for e in EXAMPLES]
        start = (datetime.now(UTC) - timedelta(days=round(365.25 * years))).date()
        key = ("whatif", years, amount, every, day, tuple((h["symbol"], h["weight"]) for h in inv.holdings))

        def load():
            symbols = sorted({h["symbol"] for m in mixes for h in m["holdings"]})
            fetch_from = datetime.combine(start, datetime.min.time(), UTC) - timedelta(days=7)
            prices = {s: data.bars(s, "1Day", fetch_from) for s in symbols}
            out = []
            for m in mixes:
                plan = InvestConfig(enabled=True, amount=amount, every=every, day=day, holdings=m["holdings"])
                try:
                    r = whatif.simulate({s: prices[s] for s in plan.symbols}, plan, start)
                except Exception as e:  # noqa: BLE001 - one mix without data shouldn't hide the others
                    log.warning("what-if %s failed: %s", m["id"], e)
                    r = None
                out.append({**m, "result": r, "short": bool(r and r["from"] > (start + timedelta(days=10)).isoformat())})
            return out

        return {"years": years, "amount": amount, "every": every, "day": day, "start": start.isoformat(),
                "mixes": slow_cache.get(key, load)}

    @app.put("/api/invest", dependencies=[Depends(guard)])
    def put_invest(body: InvestBody, request: Request) -> dict[str, Any]:
        """Save the invest plan. Turning on real buying needs `confirm`: the exact amount and schedule
        on paper, plus the typed phrase on a live account."""
        current = rt.settings
        owners_now = {s: f"strategy {i + 1} ({sc.name})" for i, sc in enumerate(current.strategies)
                      if sc.enabled for s in sc.symbols}
        try:
            new = parse_invest(dict(body.plan), owners_now)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        if new.enabled and not new.start:
            # The plan's first day: missed days before it are never "caught up", and a lump sum
            # saved again later with a new start is a new lump sum.
            new = replace(new, start=datetime.now(NY).date().isoformat())
        real = new.enabled and not new.preview
        if real and current.mode == "live":
            mac_only(request)  # starting or changing real-money buying is never done from a phone
        was_real = current.invest.enabled and not current.invest.preview
        bigger = new.amount > current.invest.amount or new.holdings != current.invest.holdings \
            or new.every != current.invest.every
        if real and (not was_real or bigger):
            want = invest_confirm_phrase(new, current.mode)
            if (body.confirm or "").strip() != want:
                raise HTTPException(422, f'To buy for real, confirm with "{want}".')
        if new.symbols and rt.broker is not None:
            bad = []
            for sym in new.symbols:
                try:
                    tradable, fractionable = rt.broker.asset_status(sym)
                except Exception:  # noqa: BLE001 - unknown symbol, or Alpaca unreachable
                    bad.append(f"{sym} wasn't found at Alpaca")
                    continue
                if not tradable:
                    bad.append(f"{sym} isn't tradable at Alpaca")
                elif not fractionable:
                    bad.append(f"{sym} can't be bought in fractional shares")
            if bad:
                raise HTTPException(422, "; ".join(bad) + ".")
        save(replace(current, invest=new))
        what = ("off" if not new.enabled else "preview" if new.preview else f"buying ${new.amount:,.2f} {new.every}")
        rt.journal.event("invest", f"invest plan saved from the dashboard: {what}",
                         "warning" if real and current.mode == "live" else "info")
        return invest_status()

    @app.post("/api/secrets", dependencies=[Depends(guard), Depends(mac_only)])
    def put_secret(body: SecretBody) -> dict[str, Any]:
        if body.name not in SETTABLE:
            raise HTTPException(422, f"{body.name} can't be set here.")
        try:
            where = set_secret(body.name, body.value.get_secret_value(), rt.dotenv_path)
        except SecretsError as e:
            raise HTTPException(422, str(e)) from None
        rt.journal.event("settings", f"{body.name} saved to the {where}")
        changed()
        return setup()

    @app.delete("/api/secrets/{name}", dependencies=[Depends(guard), Depends(mac_only)])
    def remove_secret(name: str) -> dict[str, Any]:
        if name not in SETTABLE:
            raise HTTPException(422, f"{name} can't be removed here.")
        delete_secret(name, rt.dotenv_path)
        rt.journal.event("settings", f"{name} removed")
        changed()
        return setup()

    @app.post("/api/alpaca/test", dependencies=[Depends(guard), Depends(mac_only)])
    def alpaca_test(body: AlpacaTestBody) -> dict[str, Any]:
        """Connect with the given keys (or the stored ones) and say which account they open."""
        return test_keys(body.mode, body.key_id, body.secret_key)

    @app.post("/api/mode", dependencies=[Depends(guard)])
    def set_mode(body: ModeBody, request: Request) -> dict[str, Any]:
        """Dry run / paper / live. Live needs the typed phrase and live keys that open an active account."""
        if body.mode == "live":
            mac_only(request)
        if not rt.configured:
            raise HTTPException(409, "Finish setup first.")
        cfg = rt.settings
        if body.mode == "live":
            if body.confirm.strip() != GO_LIVE_PHRASE:
                raise HTTPException(422, f'Type "{GO_LIVE_PHRASE}" to switch to real money.')
            info = test_keys("live")
            if info.get("status") != "ACTIVE":
                raise HTTPException(422, f"Your live account's status is {info.get('status')}, not ACTIVE.")
            set_secret(LIVE_CONFIRM_VAR, "1", rt.dotenv_path)
            # A plan confirmed for paper money doesn't carry over to real money: it goes back to
            # preview, and buying for real needs the "LIVE BUY ..." confirmation.
            inv = replace(cfg.invest, preview=True) if cfg.invest.enabled else cfg.invest
            new = replace(cfg, mode="live", dry_run=False, invest=inv)
        else:
            delete_secret(LIVE_CONFIRM_VAR, rt.dotenv_path)
            new = replace(cfg, mode="paper", dry_run=body.mode == "dry-run")
        before = describe_mode(cfg.mode, cfg.dry_run)
        save(new)
        after = describe_mode(new.mode, new.dry_run)
        rt.journal.event("mode", f"{before} -> {after} (from the dashboard)",
                         "warning" if new.mode == "live" else "info")
        return status()

    @app.post("/api/pause", dependencies=[Depends(guard)])
    def pause(body: PauseBody) -> dict[str, Any]:
        """The kill switch: no new buys while paused. Sells and stop-losses keep working."""
        path = kill_file(rt.settings)
        if body.paused:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"paused from the dashboard at {datetime.now(UTC).isoformat()}\n")
        else:
            path.unlink(missing_ok=True)
        rt.journal.event("pause" if body.paused else "resume",
                         "new buys paused from the dashboard" if body.paused else "resumed from the dashboard")
        return status()

    # --- selling by hand ---------------------------------------------------------------------------
    # These are real orders even in dry-run: dry-run stops the *bot* from trading, not you.

    def sell(broker, symbol: str, pos, reason: str) -> dict[str, Any]:
        order = broker.close_position(symbol, reason)
        rt.journal.record_order(
            mode=broker.mode, strategy="dashboard", symbol=symbol, side="sell", status="submitted",
            qty=pos.qty, est_price=pos.market_value / pos.qty if pos.qty else None, reason=reason,
            broker_order_id=str(getattr(order, "id", "")) or None)
        return {"symbol": symbol, "qty": pos.qty}

    @app.post("/api/positions/sell", dependencies=[Depends(guard)])
    def sell_position(body: SellBody) -> dict[str, Any]:
        broker = need_broker()
        symbol = body.symbol.strip().upper()
        pos = _broker_call(broker.snapshot).positions.get(symbol)
        if pos is None:
            raise HTTPException(404, f"You don't hold {symbol} right now.")
        if symbol in _broker_call(broker.symbols_selling):
            raise HTTPException(409, f"{symbol} already has a sell waiting to fill.")
        try:
            sold = sell(broker, symbol, pos, "sold by you from the dashboard")
        except Exception as e:  # noqa: BLE001 - shown to the user
            raise HTTPException(502, f"Alpaca didn't accept the sell for {symbol}: {str(e)[:200]}") from None
        rt.journal.event("manual_sell", f"{symbol}: sell all {pos.qty:g} shares from the dashboard", "warning")
        cache.clear()
        return sold

    @app.post("/api/positions/sell-all", dependencies=[Depends(guard)])
    def sell_everything(body: SellAllBody) -> dict[str, Any]:
        """Pause new buys, cancel every open order, then sell every position in the account.

        Cancelling comes first: done last, it would also cancel the sells themselves, which wait
        as open orders until the next open when the market is closed.
        """
        if body.confirm.strip() != SELL_ALL_PHRASE:
            raise HTTPException(422, f"Type {SELL_ALL_PHRASE} to confirm.")
        broker = need_broker()
        path = kill_file(rt.settings)  # first, so the bot can't buy back in while this runs
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"paused by sell everything at {datetime.now(UTC).isoformat()}\n")
        sold, failed = [], []
        try:
            broker.cancel_all_orders()
        except Exception as e:  # noqa: BLE001 - the sells still try; close_position cancels per symbol too
            failed.append({"symbol": "open orders", "error": f"couldn't cancel: {str(e)[:200]}"})
        positions = _broker_call(broker.snapshot).positions
        for symbol, pos in sorted(positions.items()):
            try:
                sold.append(sell(broker, symbol, pos, "sell everything, from the dashboard"))
            except Exception as e:  # noqa: BLE001 - keep going: one refusal mustn't stop the rest
                failed.append({"symbol": symbol, "error": str(e)[:200]})
        names = ", ".join(s["symbol"] for s in sold) or "nothing"
        rt.journal.event("sell_all", f"sell everything from the dashboard: selling {names}; new buys paused"
                         + (f"; {len(failed)} failed" if failed else ""), "error" if failed else "warning")
        cache.clear()
        return {"sold": sold, "failed": failed, "paused": True}

    @app.post("/api/alerts/test", dependencies=[Depends(guard), Depends(mac_only)])
    def alerts_test() -> dict[str, Any]:
        from taylortrader.alerts import Level
        from taylortrader.app import build_alerts

        alerts, notes = build_alerts(rt.settings)
        if not alerts.push and not alerts.email:
            raise HTTPException(422, "No alert channel is set up yet.")
        alerts.send("TaylorTrader test alert", "If you can read this, alerts work.", Level.WARNING, email_copy=True)
        return {"sent": True, "channels": notes}

    @app.post("/api/ai/test", dependencies=[Depends(guard), Depends(mac_only)])
    def ai_test() -> dict[str, Any]:
        """One tiny call to the configured model: proves the key, model name and JSON output work."""
        from taylortrader import llm
        from taylortrader.strategies.ai import SYSTEM

        a = rt.settings.ai
        try:
            client = llm.build(a.provider, a.model, a.endpoint, a.effort, min(a.timeout_seconds, 60), rt.dotenv_path)
            d = client.ask(SYSTEM, "Connection test from the TaylorTrader dashboard. Nothing to decide: answer hold, "
                                   "confidence 0, summary \"Connection works.\", with empty pros and cons.",
                           ("buy", "hold"))
        except llm.LLMError as e:
            raise HTTPException(422, str(e)) from None
        except Exception as e:  # noqa: BLE001 - e.g. no credentials found at all
            raise HTTPException(422, f"{type(e).__name__}: {str(e)[:200]}") from None
        return {"ok": True, "model": d.model, "latency_ms": d.latency_ms, "summary": d.summary,
                "input_tokens": d.input_tokens, "output_tokens": d.output_tokens}

    @app.post("/api/service", dependencies=[Depends(guard)])
    def service(body: ServiceBody) -> dict[str, Any]:
        if not launchd.supported():
            raise HTTPException(501, "Starting the background service from here works on macOS only. "
                                     "Run `taylortrader run` in a terminal instead.")
        if body.action != "stop" and not (rt.configured and rt.settings.enabled_strategies):
            raise HTTPException(409, "Pick at least one strategy first.")
        if body.action != "stop" and rt.broker is None:
            raise HTTPException(409, rt.broker_error or "Connect Alpaca first.")
        try:
            {"start": lambda: launchd.start(project), "stop": launchd.stop, "restart": launchd.restart}[body.action]()
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from None
        rt.journal.event("service", f"background service: {body.action} (from the dashboard)")
        return setup()

    @app.post("/api/backtest", dependencies=[Depends(guard)])
    def backtest_preview(body: BacktestBody) -> dict[str, Any]:
        """One strategy on history (Alpaca SIP, every exchange), next to buy-and-hold."""
        from taylortrader import backtest
        from taylortrader.data import MarketData
        from taylortrader.secrets import load_alpaca_credentials
        from taylortrader.strategies import get_strategy

        clean_symbol(body.symbol)
        if body.timeframe not in TIMEFRAMES:
            raise HTTPException(422, f"timeframe must be one of {TIMEFRAMES}")
        days = min(body.days, 60) if body.timeframe == "1Min" else body.days
        try:
            strat = get_strategy(body.strategy, body.params)
            creds = load_alpaca_credentials("paper", rt.dotenv_path)  # history only; never live keys
        except (ValueError, SecretsError) as e:
            raise HTTPException(422, str(e)) from None
        md = MarketData(creds, rt.settings.data_dir / "cache", feed="sip")
        symbol = body.symbol.strip().upper()
        df = _broker_call(lambda: md.bars(symbol, body.timeframe, datetime.now(UTC) - timedelta(days=days)))
        if len(df) <= strat.warmup + 2:
            raise HTTPException(422, f"Not enough {body.timeframe} history for {symbol} ({len(df)} bars).")
        try:
            r = backtest.run(df, strat, allocation_pct=body.allocation_pct, stop_loss_pct=body.stop_loss_pct,
                             take_profit_pct=body.take_profit_pct, cash_yield_pct=CASH_YIELD)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        closes = df["close"].loc[r.equity.index]
        return {
            "summary": r.summary(),
            "symbol": symbol,
            "timeframe": body.timeframe,
            "equity": _downsample(r.equity / r.equity.iloc[0] * 100),
            "buy_hold": _downsample(closes / closes.iloc[0] * 100),
        }

    # --- the page --------------------------------------------------------------------------------

    @app.middleware("http")
    async def fresh_page(request: Request, call_next):
        """The page must be re-checked on every load, or an update shows the old dashboard until a
        hard refresh. Built assets have content hashes in their names, so those can be cached."""
        response = await call_next(request)
        if not request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Frame-Options"] = "DENY"  # no clickjacking: this page is never framed
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.exception_handler(404)
    async def not_found(request, exc):
        if request.url.path.startswith("/api/") or not (STATIC / "index.html").exists():
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(STATIC / "index.html")

    if (STATIC / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    else:
        @app.get("/")
        def no_ui():
            return JSONResponse({"detail": "Dashboard not built. Run: cd dashboard && npm install && npm run build"})

    return app
