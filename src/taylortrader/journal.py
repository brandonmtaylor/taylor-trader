"""Trade journal and event log in SQLite (var/journal.db, git-ignored).

Every order the bot places is recorded with the reason it was placed. Heartbeats live here too,
so the watchdog (a separate process) can tell whether the service is alive.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,              -- paper | live | dry-run
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy | sell
    qty REAL,
    est_price REAL,
    stop_price REAL,
    take_profit_price REAL,
    reason TEXT,
    broker_order_id TEXT,
    status TEXT NOT NULL,            -- submitted | blocked | filled | failed
    fill_price REAL,
    fill_ts TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS invest_orders (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    period TEXT NOT NULL,            -- "2026-11", "week-2026-11-02", "once", ...
    mode TEXT NOT NULL,              -- preview | paper | live
    symbol TEXT NOT NULL,
    notional REAL NOT NULL,          -- dollars
    planned_total REAL,              -- the contribution this was part of
    status TEXT NOT NULL,            -- planned | preview | submitted | blocked | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    order_id INTEGER,                -- orders.id once sent
    detail TEXT,
    UNIQUE (period, mode, symbol)    -- one buy per symbol per period: never twice
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_trades (
    id INTEGER PRIMARY KEY,
    buy_date TEXT, sell_date TEXT, symbol TEXT, strategy TEXT,
    buy_price REAL, sell_price REAL, shares REAL, percent_change REAL, profit REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS orders_symbol ON orders(symbol, ts);
-- Every question put to an AI strategy, whatever the answer (including holds and errors).
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    question TEXT NOT NULL,          -- entry | exit
    action TEXT,                     -- buy | sell | hold (null on error)
    confidence REAL,
    summary TEXT,
    pros TEXT,                       -- JSON list
    cons TEXT,                       -- JSON list
    acted INTEGER NOT NULL DEFAULT 0, -- 1 if it became an order signal
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    error TEXT,
    features TEXT                    -- JSON: the indicator values the model was shown
);
CREATE INDEX IF NOT EXISTS ai_decisions_ts ON ai_decisions(ts);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._add_columns()

    def _add_columns(self) -> None:
        """Columns added after the first release, for journals created before them."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(ai_decisions)")}
        if "features" not in have:
            self.db.execute("ALTER TABLE ai_decisions ADD COLUMN features TEXT")

    # --- orders --------------------------------------------------------------------------------

    def record_order(self, *, mode: str, strategy: str, symbol: str, side: str, status: str,
                     qty: float | None = None, est_price: float | None = None,
                     stop_price: float | None = None, take_profit_price: float | None = None,
                     reason: str = "", broker_order_id: str | None = None, detail: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO orders (ts, mode, strategy, symbol, side, qty, est_price, stop_price, "
            "take_profit_price, reason, broker_order_id, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), mode, strategy, symbol, side, qty, est_price, stop_price, take_profit_price,
             reason, broker_order_id, status, detail),
        )
        return cur.lastrowid

    def sold_since(self, symbol: str, cutoff_iso: str) -> bool:
        """Did the bot (or the dashboard) sell this symbol recently? Recent means placed since the
        cutoff, still waiting, or filled since the cutoff (a sell sent after hours fills at the open).
        fill_ts and ts are formatted differently, so compare them as datetimes."""
        return self.db.execute(
            "SELECT 1 FROM orders WHERE symbol=? AND side='sell' AND (ts >= ? OR status='submitted'"
            " OR (fill_ts IS NOT NULL AND datetime(fill_ts) >= datetime(?)))",
            (symbol, cutoff_iso, cutoff_iso)).fetchone() is not None

    def pending_orders(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM orders WHERE status = 'submitted' AND broker_order_id IS NOT NULL"))

    def mark_filled(self, row_id: int, fill_price: float, fill_ts: str) -> None:
        self.db.execute("UPDATE orders SET status='filled', fill_price=?, fill_ts=? WHERE id=?",
                        (fill_price, fill_ts, row_id))

    def mark_status(self, row_id: int, status: str, detail: str = "") -> None:
        self.db.execute("UPDATE orders SET status=?, detail=? WHERE id=?", (status, detail, row_id))

    def orders_since(self, ts_iso: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM orders WHERE ts >= ? ORDER BY ts", (ts_iso,)))

    def recent_orders(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)))

    # --- invest plan ----------------------------------------------------------------------------

    def plan_invest(self, period: str, mode: str, alloc: dict[str, float], planned_total: float) -> None:
        """Write a contribution's buys before any is sent. A second call for the same period adds nothing."""
        ts = now_iso()
        with self.db:
            self.db.execute("BEGIN")
            for symbol, notional in alloc.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO invest_orders (ts, period, mode, symbol, notional, planned_total, status)"
                    " VALUES (?,?,?,?,?,?, 'planned')", (ts, period, mode, symbol, notional, planned_total))

    def invest_rows(self, period: str, mode: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM invest_orders WHERE period=? AND mode=? ORDER BY id",
                                    (period, mode)))

    def invest_mark(self, row_id: int, status: str, detail: str = "", order_row: int | None = None,
                    attempt: bool = False) -> None:
        self.db.execute("UPDATE invest_orders SET status=?, detail=?, order_id=COALESCE(?, order_id), "
                        "attempts=attempts+? WHERE id=?", (status, detail, order_row, int(attempt), row_id))

    def invest_periods(self, mode: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT period FROM invest_orders WHERE mode=?", (mode,))}

    def invest_put_in(self, mode: str) -> float:
        """Dollars the plan has sent to the broker, less orders Alpaca canceled or rejected."""
        r = self.db.execute(
            "SELECT COALESCE(SUM(i.notional), 0) FROM invest_orders i LEFT JOIN orders o ON o.id = i.order_id "
            "WHERE i.mode=? AND i.status='submitted' AND COALESCE(o.status, '') NOT IN "
            "('canceled', 'expired', 'rejected')", (mode,)).fetchone()
        return float(r[0])

    def invest_history(self, limit: int = 500) -> list[dict]:
        """Contributions, newest first: one per period and mode, with each symbol's buy and its order status."""
        rows = self.db.execute(
            "SELECT i.*, o.status AS order_status FROM invest_orders i LEFT JOIN orders o ON o.id = i.order_id "
            "ORDER BY i.id DESC LIMIT ?", (limit,))
        out: dict[tuple[str, str], dict] = {}
        for r in rows:
            e = out.setdefault((r["period"], r["mode"]), {
                "period": r["period"], "mode": r["mode"], "ts": r["ts"], "total": 0.0,
                "planned_total": r["planned_total"], "buys": []})
            status = r["order_status"] or r["status"]
            e["buys"].append({"symbol": r["symbol"], "notional": r["notional"], "status": status,
                              "detail": r["detail"] or ""})
            if status in ("submitted", "filled", "preview"):
                e["total"] += r["notional"]
        return list(out.values())

    # --- events / heartbeat --------------------------------------------------------------------

    def event(self, kind: str, message: str, level: str = "info") -> None:
        self.db.execute("INSERT INTO events (ts, level, kind, message) VALUES (?,?,?,?)",
                        (now_iso(), level, kind, message))

    def events_since(self, ts_iso: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts", (ts_iso,)))

    def beat(self, status: str = "ok") -> None:
        self.db.execute(
            "INSERT INTO heartbeat (id, ts, status) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, status=excluded.status",
            (now_iso(), status),
        )

    def last_beat(self) -> tuple[datetime, str] | None:
        row = self.db.execute("SELECT ts, status FROM heartbeat WHERE id = 1").fetchone()
        return (datetime.fromisoformat(row["ts"]), row["status"]) if row else None

    # --- AI decisions ----------------------------------------------------------------------------

    def record_ai(self, *, strategy: str, symbol: str, question: str, action: str | None = None,
                  confidence: float | None = None, summary: str = "", pros: list[str] | None = None,
                  cons: list[str] | None = None, acted: bool = False, model: str = "",
                  input_tokens: int | None = None, output_tokens: int | None = None,
                  latency_ms: int | None = None, error: str | None = None,
                  features: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO ai_decisions (ts, strategy, symbol, question, action, confidence, summary, pros, cons,"
            " acted, model, input_tokens, output_tokens, latency_ms, error, features)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), strategy, symbol, question, action, confidence, summary, json.dumps(pros or []),
             json.dumps(cons or []), int(acted), model, input_tokens, output_tokens, latency_ms, error,
             json.dumps(features) if features else None),
        )

    def ai_calls_since(self, ts_iso: str) -> int:
        """Model calls made (answered or failed), for the daily cap."""
        return self.db.execute("SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND model != 'skipped'",
                               (ts_iso,)).fetchone()[0]

    def recent_ai(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM ai_decisions ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "pros": json.loads(r["pros"] or "[]"), "cons": json.loads(r["cons"] or "[]"),
                 "features": json.loads(r["features"]) if r["features"] else None}
                for r in rows]

    # --- legacy import -------------------------------------------------------------------------

    def import_legacy(self, legacy_db: Path) -> int:
        """Import closed and open trades from the original bot's transactions DB."""
        src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        rows = list(src.execute("SELECT * FROM transactions"))
        src.close()
        source = legacy_db.name
        self.db.execute("DELETE FROM legacy_trades WHERE source = ?", (source,))  # idempotent
        for r in rows:
            keys = r.keys()

            def get(k, keys=keys, r=r):
                return r[k] if k in keys and r[k] != "" else None

            self.db.execute(
                "INSERT INTO legacy_trades (buy_date, sell_date, symbol, strategy, buy_price, sell_price, "
                "shares, percent_change, profit, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (get("buy_date"), get("sell_date"), get("stock"), get("strategy"), get("buy_price"),
                 get("sell_price"), get("total_num_shares_bought"), get("percent_increase"),
                 get("profit"), source),
            )
        return len(rows)
