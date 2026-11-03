"""Read-only dashboard API. Listens on 127.0.0.1 only and has no endpoints that change anything.

Controls (kill switch, settings, paper/live) come in M2, behind their own confirmation flow.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from guardrails import find_unmanaged_positions
from taylortrader.config import Config
from taylortrader.journal import Journal
from taylortrader.service import kill_file

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PERIODS = {"1M": 31, "3M": 92, "1A": 366}
BENCHMARK = "SPY"


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


def _broker_call(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001  any broker failure becomes a clear 503
        log.warning("broker call failed: %s", e)
        raise HTTPException(503, f"Can't reach Alpaca: {type(e).__name__}: {e}") from None


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def create_app(cfg: Config, broker, market_data, journal: Journal, alert_notes: list[str],
               cache_ttl: float = 10.0) -> FastAPI:
    app = FastAPI(title="TaylorTrader", docs_url=None, redoc_url=None, openapi_url=None)
    # Reject requests whose Host isn't this machine (blocks DNS-rebinding from web pages).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    cache = TTLCache(cache_ttl)
    owners = {s: sc.name for sc in cfg.enabled_strategies for s in sc.symbols}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        beat = journal.last_beat()
        now = datetime.now(UTC)
        age = (now - beat[0]).total_seconds() if beat else None
        clock = cache.get("clock", lambda: _broker_call(broker.clock))
        return {
            "mode": cfg.mode,
            "running_mode": beat[1] if beat else None,  # "ok" or "dry-run"
            "heartbeat": {
                "ts": _iso(beat[0]) if beat else None,
                "age_s": age,
                "stale": age is None or age > cfg.service.heartbeat_stale_minutes * 60,
                "stale_after_s": cfg.service.heartbeat_stale_minutes * 60,
            },
            "kill_switch": kill_file(cfg).exists(),
            "market": {"is_open": clock.is_open, "next_open": _iso(clock.next_open),
                       "next_close": _iso(clock.next_close)},
            "strategies": [
                {"name": sc.name, "symbols": sc.symbols, "timeframe": sc.timeframe,
                 "allocation_pct": sc.allocation_pct, "stop_loss_pct": sc.stop_loss_pct,
                 "take_profit_pct": sc.take_profit_pct, "enabled": sc.enabled, "params": sc.params}
                for sc in cfg.strategies
            ],
            "limits": vars(cfg.limits),
            "alerts": alert_notes,
            "server_time": now.isoformat(),
        }

    @app.get("/api/account")
    def account() -> dict[str, Any]:
        def load():
            snap = _broker_call(broker.snapshot)
            stops = _broker_call(broker.symbols_with_stop)
            return snap, stops

        snap, stops = cache.get("account", load)
        problems = find_unmanaged_positions(snap.positions, stops, owners)
        positions = []
        for p in sorted(snap.positions.values(), key=lambda p: -abs(p.market_value)):
            price = p.market_value / p.qty if p.qty else 0.0
            positions.append({
                "symbol": p.symbol, "qty": p.qty, "market_value": p.market_value,
                "pct_of_equity": p.market_value / snap.equity * 100 if snap.equity else 0,
                "avg_entry_price": p.avg_entry_price, "price": price,
                "unrealized_pct": (price / p.avg_entry_price - 1) * 100 if p.avg_entry_price else None,
                "has_stop": p.symbol in stops, "owner": owners.get(p.symbol),
                "issues": problems.get(p.symbol, []),
            })
        return {
            "equity": snap.equity, "cash": snap.cash, "day_start_equity": snap.day_start_equity,
            "day_pnl_pct": snap.day_pnl_pct, "trades_today": snap.trades_today,
            "exposure_pct": snap.exposure / snap.equity * 100 if snap.equity else 0,
            "positions": positions,
        }

    @app.get("/api/performance")
    def performance(period: str = Query("1M", pattern="^(1M|3M|1A)$")) -> dict[str, Any]:
        def load():
            equity = _broker_call(lambda: broker.portfolio_history(period))
            bench: list[tuple[int, float]] = []
            if equity:
                start = datetime.fromtimestamp(equity[0][0], UTC) - timedelta(days=5)
                try:
                    df = market_data.bars(BENCHMARK, "1Day", start)
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
        return [dict(r) for r in journal.recent_orders(limit)]

    @app.get("/api/events")
    def events(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        rows = journal.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @app.get("/api/history")
    def history() -> dict[str, Any]:
        rows = [dict(r) for r in journal.db.execute(
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
