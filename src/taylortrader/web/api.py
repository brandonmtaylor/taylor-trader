"""Dashboard API. Listens on 127.0.0.1 only.

Reads are GETs. Every change (settings, keys, mode, pause, service) is a POST/PUT/DELETE that
must carry the `X-TaylorTrader: 1` header and an Origin of this same address:
- a custom header forces a CORS preflight, which this server never approves, so another
  website open in your browser can't send one;
- the Origin check refuses anything that did get through from elsewhere;
- TrustedHostMiddleware refuses other Host names (DNS rebinding).
Secret values go in and never come back out: the API reports only whether, and where, each is set.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
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
    save_config,
)
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
from taylortrader.web.runtime import Runtime

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PROJECT = Path(__file__).resolve().parents[3]
BENCHMARK = "SPY"
GO_LIVE_PHRASE = "GO LIVE"
SELL_ALL_PHRASE = "SELL ALL"
WRITE_HEADER = "x-taylortrader"
FORBIDDEN = "Changes must come from the TaylorTrader dashboard."


class TTLCache:
    """Tiny cache so a browser polling every few seconds doesn't hammer Alpaca's rate limit."""

    def __init__(self, ttl: float):
        self.ttl, self._data = ttl, {}

    def get(self, key: str, fn):
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = fn()
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


def create_app(rt: Runtime, cache_ttl: float = 10.0, project: Path = PROJECT) -> FastAPI:
    app = FastAPI(title="TaylorTrader", docs_url=None, redoc_url=None, openapi_url=None)
    # Reject requests whose Host isn't this machine (blocks DNS-rebinding from web pages).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
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
            "secret_store": "Keychain" if keychain_available() else ".env",
            "secrets": {n: secret_source(n, rt.dotenv_path) for n in SETTABLE},
            "service": launchd.status(),
        }

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
        problems = find_unmanaged_positions(snap.positions, stops, own)
        positions = []
        for p in sorted(snap.positions.values(), key=lambda p: -abs(p.market_value)):
            price = p.market_value / p.qty if p.qty else 0.0
            positions.append({
                "symbol": p.symbol, "qty": p.qty, "market_value": p.market_value,
                "pct_of_equity": p.market_value / snap.equity * 100 if snap.equity else 0,
                "avg_entry_price": p.avg_entry_price, "price": price,
                "unrealized_pct": (price / p.avg_entry_price - 1) * 100 if p.avg_entry_price else None,
                "has_stop": p.symbol in stops, "owner": own.get(p.symbol),
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
        from taylortrader import chart

        broker = need_broker()
        fills, strategy_of = fills_and_owners()
        held = cache.get("snapshot", lambda: _broker_call(broker.snapshot)).positions
        # Stocks worth picking: held now, traded this year, or watched by a strategy.
        recent = [f.symbol for f in sorted(fills, key=lambda f: f.ts, reverse=True)]
        watched = [s for sc in rt.settings.enabled_strategies for s in sc.symbols]
        symbols = [s for s in dict.fromkeys([*held, *recent, *watched]) if "/" not in s]  # stocks, not crypto
        symbol = symbol.strip().upper() or (symbols[0] if symbols else "")
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
            "configured": rt.configured,
            "catalog": list_strategies(),
            "hard_limits": asdict(HARD_LIMITS),
            "timeframes": list(TIMEFRAMES),
            "feeds": list(FEEDS),
        }

    # --- writes ----------------------------------------------------------------------------------

    @app.put("/api/settings", dependencies=[Depends(guard)])
    def put_settings(body: SettingsBody) -> dict[str, Any]:
        """Strategies, limits, alerts, data feed. Mode is never changed here: see /api/mode."""
        current = rt.settings
        raw = dict(body.config)
        raw.update(mode=current.mode, dry_run=current.dry_run, data_dir=str(current.data_dir))
        try:
            new = parse_config(raw)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        save(new)
        rt.journal.event("settings", "settings saved from the dashboard")
        return settings()

    @app.post("/api/secrets", dependencies=[Depends(guard)])
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

    @app.delete("/api/secrets/{name}", dependencies=[Depends(guard)])
    def remove_secret(name: str) -> dict[str, Any]:
        if name not in SETTABLE:
            raise HTTPException(422, f"{name} can't be removed here.")
        delete_secret(name, rt.dotenv_path)
        rt.journal.event("settings", f"{name} removed")
        changed()
        return setup()

    @app.post("/api/alpaca/test", dependencies=[Depends(guard)])
    def alpaca_test(body: AlpacaTestBody) -> dict[str, Any]:
        """Connect with the given keys (or the stored ones) and say which account they open."""
        return test_keys(body.mode, body.key_id, body.secret_key)

    @app.post("/api/mode", dependencies=[Depends(guard)])
    def set_mode(body: ModeBody) -> dict[str, Any]:
        """Dry run / paper / live. Live needs the typed phrase and live keys that open an active account."""
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
            new = replace(cfg, mode="live", dry_run=False)
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

    @app.post("/api/alerts/test", dependencies=[Depends(guard)])
    def alerts_test() -> dict[str, Any]:
        from taylortrader.alerts import Level
        from taylortrader.app import build_alerts

        alerts, notes = build_alerts(rt.settings)
        if not alerts.push and not alerts.email:
            raise HTTPException(422, "No alert channel is set up yet.")
        alerts.send("TaylorTrader test alert", "If you can read this, alerts work.", Level.WARNING, email_copy=True)
        return {"sent": True, "channels": notes}

    @app.post("/api/ai/test", dependencies=[Depends(guard)])
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
                             take_profit_pct=body.take_profit_pct)
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
