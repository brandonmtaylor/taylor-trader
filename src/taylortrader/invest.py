"""The long-term invest plan: buy a fixed mix of funds on a schedule and hold it.

New money does the rebalancing: each contribution goes to whatever is furthest under its target
weight first, so the plan drifts back toward its weights without selling (no taxes, no fees on
sells). Orders are fractional dollar amounts, so $200 splits exactly.

Safety:
- Each contribution is planned once per period and written to the journal before any order goes
  out, and every order carries a client order id made from the period and symbol. A restart, a
  crash between "sent" and "recorded", or a second copy of the service can't buy a month twice:
  Alpaca refuses a repeated client order id, and the bot then looks up the order it already placed.
- Buys go through the investing guardrails (guardrails.invest): kill switch, plan symbols only,
  no margin, a size cap. No stop-loss and no daily loss limit, on purpose.
- Preview mode runs the whole schedule and alerts what it would buy, without ordering. Preview
  contributions are recorded separately, so turning preview off still buys that period for real.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from taylortrader.alerts import Level
from taylortrader.config import InvestConfig
from taylortrader.guardrails.invest import MIN_ORDER, check_invest_buy

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
BUY_AFTER = (10, 0)        # New York time: let the opening rush settle first
EPOCH = date(2024, 1, 1)   # a Monday: anchors the every-two-weeks schedule
MAX_ATTEMPTS = 3
STRATEGY = "invest"        # the name plan orders carry in the journal


# Starting points the user picks from and edits. Examples of common simple mixes, not advice:
# nothing here asks about the user or picks one for them.
EXAMPLES = [
    {"id": "three-80", "name": "Three funds, mostly stocks",
     "about": "80% stocks (US and international), 20% bonds. Grows more, swings more.",
     "holdings": [{"symbol": "VTI", "weight": 55.0}, {"symbol": "VXUS", "weight": 25.0},
                  {"symbol": "BND", "weight": 20.0}]},
    {"id": "three-60", "name": "Three funds, balanced",
     "about": "60% stocks, 40% bonds. The classic middle ground.",
     "holdings": [{"symbol": "VTI", "weight": 40.0}, {"symbol": "VXUS", "weight": 20.0},
                  {"symbol": "BND", "weight": 40.0}]},
    {"id": "world", "name": "One fund, the whole world",
     "about": "Every major stock market in one fund. Simplest, all stocks.",
     "holdings": [{"symbol": "VT", "weight": 100.0}]},
    {"id": "conservative", "name": "Conservative",
     "about": "30% stocks, 50% bonds, 20% short-term Treasury bills. Steadier, grows less.",
     "holdings": [{"symbol": "VTI", "weight": 20.0}, {"symbol": "VXUS", "weight": 10.0},
                  {"symbol": "BND", "weight": 50.0}, {"symbol": "SGOV", "weight": 20.0}]},
]


# --- schedule ------------------------------------------------------------------------------------

def due_period(inv: InvestConfig, today: date) -> str | None:
    """The contribution period that's due on `today`, or None. A period is due from its day
    onward until the next one's day, so a missed day (weekend, holiday, Mac asleep) catches up,
    even across a month or week boundary (day 28 on a Saturday is bought on the next Monday).
    The previous period only counts if the plan had started before the current period began."""
    start = date.fromisoformat(inv.start) if inv.start else None
    if start and today < start:
        return None
    if inv.until and today > date.fromisoformat(inv.until):
        return None
    if inv.every == "once":
        return f"once-{inv.start}" if inv.start else "once"  # a new start date is a new lump sum
    if inv.every == "month":
        if today.day >= inv.day:
            return f"{today:%Y-%m}"
        first = today.replace(day=1)
        return f"{first - timedelta(days=1):%Y-%m}" if start and start < first else None
    if inv.every == "week":
        monday = today - timedelta(days=today.weekday())
        if today.weekday() >= inv.day:
            return f"week-{monday}"
        return f"week-{monday - timedelta(days=7)}" if start and start < monday else None
    if inv.every == "2weeks":
        first = EPOCH + timedelta(days=(today - EPOCH).days // 14 * 14)
        if today >= first + timedelta(days=inv.day):
            return f"2weeks-{first}"
        return f"2weeks-{first - timedelta(days=14)}" if start and start < first else None
    raise ValueError(f"unknown schedule {inv.every!r}")


def cents(v: float) -> float:
    """Round down to whole cents, without float noise turning $4.35 into $4.34."""
    return math.floor(v * 100 + 1e-6) / 100


def next_due(inv: InvestConfig, today: date, done: set[str],
             open_days: set[date] | None = None) -> tuple[date, str] | None:
    """The next day a contribution will go in, skipping periods already done. None if the plan's over.
    open_days (from the market calendar) skips holidays; past its end, or without it, weekdays count."""
    known_until = max(open_days) if open_days else None
    for k in range(400):
        d = today + timedelta(days=k)
        if d.weekday() >= 5 or (known_until and d <= known_until and d not in open_days):
            continue
        p = due_period(inv, d)
        if p and p not in done:
            return d, p
        if inv.until and d > date.fromisoformat(inv.until):
            return None
    return None


# --- the math ------------------------------------------------------------------------------------

def split(amount: float, values: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    """Dollars per symbol for one contribution, sent to whatever is most under its target first.

    Target for each holding = its weight x (what the plan holds now + this contribution). Money goes
    to each holding's shortfall in proportion, so nothing ends up over its target because of new money.
    Amounts are whole cents; any under Alpaca's $1 minimum are folded into the largest.
    """
    if amount < MIN_ORDER or not weights:
        return {}
    total_w = sum(weights.values())
    target_total = sum(max(0.0, values.get(s, 0.0)) for s in weights) + amount
    gaps = {s: max(0.0, target_total * w / total_w - max(0.0, values.get(s, 0.0))) for s, w in weights.items()}
    gap_sum = sum(gaps.values())  # >= amount: the shortfalls add up to at least the new money
    raw = {s: amount * g / gap_sum for s, g in gaps.items()} if gap_sum > 0 else \
        {s: amount * w / total_w for s, w in weights.items()}
    whole = {s: cents(v) for s, v in raw.items()}
    out = {s: v for s, v in whole.items() if v >= MIN_ORDER}
    if not out:
        return {max(raw, key=raw.get): cents(amount)}
    biggest = max(out, key=out.get)
    out[biggest] = round(out[biggest] + (cents(amount) - sum(out.values())), 2)
    return out


def drift(values: dict[str, float], weights: dict[str, float]) -> dict[str, dict]:
    """Current vs. target weight per holding, in percentage points."""
    total = sum(max(0.0, values.get(s, 0.0)) for s in weights)
    tw = sum(weights.values())
    out = {}
    for s, w in weights.items():
        now = values.get(s, 0.0) / total * 100 if total > 0 else 0.0
        out[s] = {"target": w / tw * 100, "now": now, "drift": now - w / tw * 100, "value": values.get(s, 0.0)}
    return out


def plan_values(inv: InvestConfig, snap) -> dict[str, float]:
    return {s: (snap.positions[s].market_value if s in snap.positions else 0.0) for s in inv.symbols}


# --- running it ------------------------------------------------------------------------------------

class InvestRunner:
    """Called from the service loop while the market is open."""

    def __init__(self, cfg, broker, journal, alerts, guardrails, now, dry_run: bool = False):
        self.cfg, self.broker, self.journal, self.alerts = cfg, broker, journal, alerts
        self.guardrails, self.now, self.dry_run = guardrails, now, dry_run
        self._assets_ok: tuple[date, list[str]] | None = None  # (day checked, problems)
        self._calendar: tuple[date, set[date] | None] | None = None  # (day checked, open days)
        self.kill_check = lambda: False  # re-read just before a buy is sent

    def _open_days(self, today: date) -> set[date] | None:
        """Market days for the next ~13 months, checked once a day. None if the calendar can't be read."""
        if self._calendar and self._calendar[0] == today:
            return self._calendar[1]
        try:
            days = self.broker.market_days(today, today + timedelta(days=400))
        except Exception as e:  # noqa: BLE001 - weekdays are a fine stand-in
            log.info("market calendar unavailable: %s", e)
            days = None
        self._calendar = (today, days)
        return days

    @property
    def inv(self) -> InvestConfig:
        return self.cfg.invest

    @property
    def mode(self) -> str:
        return "preview" if (self.inv.preview or self.dry_run) else self.broker.mode

    def reserved(self, snap) -> float:
        """Money trading strategies shouldn't size from: what the plan holds, plus the next
        contribution (when it's investing for real and `reserve` is on)."""
        held = sum(plan_values(self.inv, snap).values()) if self.inv.symbols else 0.0
        upcoming = 0.0
        if self.inv.enabled and self.inv.reserve and self.mode != "preview":
            today = self.now().astimezone(NY).date()
            if next_due(self.inv, today, self.journal.invest_periods(self.mode), self._open_days(today)):
                upcoming = self.inv.amount
        return held + upcoming

    def _asset_problems(self, today: date) -> list[str]:
        if self._assets_ok and self._assets_ok[0] == today:
            return self._assets_ok[1]
        problems = []
        for s in self.inv.symbols:
            try:
                tradable, fractionable = self.broker.asset_status(s)
            except Exception as e:  # noqa: BLE001 - reported, and the plan waits
                problems.append(f"{s}: couldn't check ({e})")
                continue
            if not tradable:
                problems.append(f"{s} isn't tradable at Alpaca")
            elif not fractionable:
                problems.append(f"{s} can't be bought in fractional shares at Alpaca")
        self._assets_ok = (today, problems)
        return problems

    def tick(self, snap) -> None:
        inv = self.inv
        if not inv.enabled:
            return
        now_ny = self.now().astimezone(NY)
        if (now_ny.hour, now_ny.minute) < BUY_AFTER:
            return
        today = now_ny.date()
        period = due_period(inv, today)
        if period is None:
            return
        mode = self.mode
        rows = self.journal.invest_rows(period, mode)
        if not rows:
            if self.guardrails.kill_switch:
                self.alerts.send("Invest plan waiting", f"The {period} contribution is due, but the kill switch "
                                 "is on. It goes in once it's off.", Level.WARNING, key=f"invest-kill:{today}")
                return
            problems = self._asset_problems(today)
            if problems:
                self.alerts.send("Invest plan can't buy", "; ".join(problems) + ". Fix the plan's holdings; "
                                 "nothing was bought.", Level.WARNING, key=f"invest-assets:{today}")
                return
            available = inv.amount if mode == "preview" else min(inv.amount, snap.cash)
            available = cents(available)
            if available < MIN_ORDER:
                self.alerts.send("Invest plan: no cash", f"The {period} contribution of ${inv.amount:,.2f} is due, "
                                 f"but there's ${snap.cash:,.2f} cash. Deposit money at Alpaca; it'll try again "
                                 "tomorrow.", Level.WARNING, key=f"invest-cash:{today}")
                return
            alloc = split(available, plan_values(inv, snap), inv.weights)
            self.journal.plan_invest(period, mode, alloc, planned_total=inv.amount)
            rows = self.journal.invest_rows(period, mode)

        # "blocked" is retried too: a kill switch or a cash dip shouldn't lose the period's buy.
        todo = [r for r in rows if r["status"] in ("planned", "blocked")
                or (r["status"] == "failed" and r["attempts"] < MAX_ATTEMPTS)]
        if not todo:
            return
        spent = 0.0
        placed = []
        for r in todo:
            ok = self._place(r, period, mode, replace(snap, cash=snap.cash - spent))
            if ok:
                spent += r["notional"]
                placed.append((r["symbol"], r["notional"]))
        if placed:
            total = sum(n for _, n in placed)
            lines = "\n".join(f"{s} ${n:,.2f}" for s, n in placed)
            short = ""
            planned = rows[0]["planned_total"] or inv.amount
            if sum(r["notional"] for r in rows) + 0.005 < planned:
                short = (f"\nOnly ${sum(r['notional'] for r in rows):,.2f} of ${planned:,.2f}: that's all the "
                         "cash there was. Deposit more at Alpaca for the next one.")
            title = (f"[preview] Invest plan would buy ${total:,.2f}" if mode == "preview"
                     else f"[{mode}] Invest plan: buying ${total:,.2f}")
            self.journal.event("invest", f"{period} {mode}: " + ", ".join(f"{s} ${n:,.2f}" for s, n in placed))
            self.alerts.send(title, lines + short, key=f"invest:{period}:{mode}")

    def _place(self, r, period: str, mode: str, account) -> bool:
        symbol, notional = r["symbol"], r["notional"]
        if mode == "preview":
            self.journal.invest_mark(r["id"], "preview")
            return True
        if self.kill_check():
            self.guardrails.kill_switch = True
        d = check_invest_buy(symbol, notional, account, plan_symbols=self.inv.symbols,
                             max_notional=self.inv.amount, kill_switch=self.guardrails.kill_switch)
        reason = f"invest plan {period}: ${notional:,.2f}"
        if not d:
            why = "; ".join(d.reasons)
            self.journal.invest_mark(r["id"], "blocked", why)
            self.journal.record_order(mode=mode, strategy=STRATEGY, symbol=symbol, side="buy", status="blocked",
                                      reason=reason, detail=why)
            self.alerts.send(f"Invest plan: blocked {symbol}", why, Level.WARNING, key=f"invest-block:{period}:{symbol}")
            return False
        cid = f"tt-invest-{period}-{symbol}-{r['attempts'] + 1}"
        try:
            order = self.broker.submit_notional_buy(symbol, notional, cid)
        except Exception as e:  # noqa: BLE001 - maybe it got through anyway (a timeout): look before retrying
            order, checked = self._find(cid)
            if order is None and not checked:
                # Can't tell whether it went through. Keep the same order id for the next try, so if
                # it did, Alpaca refuses the duplicate instead of buying twice.
                self.journal.invest_mark(r["id"], "failed", f"unconfirmed: {str(e)[:280]}")
                self.alerts.send(f"Invest plan: {symbol} buy unconfirmed", f"{e}\nCouldn't check with Alpaca "
                                 "whether it went through. It'll check again with the same order id, so it "
                                 "can't buy twice.", Level.WARNING, key=f"invest-unconfirmed:{period}:{symbol}")
                return False
            if order is None:
                self.journal.invest_mark(r["id"], "failed", str(e)[:300], attempt=True)
                left = MAX_ATTEMPTS - r["attempts"] - 1
                self.alerts.send(f"Invest plan: {symbol} buy failed", f"{e}\n" + (
                    f"Will retry ({left} more)." if left else "Giving up on this one for this period."),
                    Level.WARNING, key=f"invest-fail:{period}:{symbol}:{r['attempts']}")
                return False
        row_id = self.journal.record_order(mode=mode, strategy=STRATEGY, symbol=symbol, side="buy",
                                           status="submitted", reason=reason, broker_order_id=str(order.id))
        self.journal.invest_mark(r["id"], "submitted", order_row=row_id, attempt=True)
        return True

    def _find(self, cid: str):
        """(order or None, whether Alpaca actually answered). None + False means "couldn't check"."""
        try:
            return self.broker.order_by_client_id(cid), True
        except Exception:  # noqa: BLE001
            return None, False

    def status(self, snap) -> dict:
        """For the daily email, the dashboard and MCP."""
        inv = self.inv
        values = plan_values(inv, snap)
        mode = self.mode
        now_ny = self.now().astimezone(NY)
        # After the close, today's chance has passed: the earliest buy is the next day.
        first = now_ny.date() + timedelta(days=1 if (now_ny.hour, now_ny.minute) >= (16, 0) else 0)
        nxt = next_due(inv, first, self.journal.invest_periods(mode), self._open_days(now_ny.date())) if inv.enabled else None
        upcoming = None
        if nxt:
            amount = inv.amount if mode == "preview" else min(inv.amount, snap.cash)
            upcoming = {"date": nxt[0].isoformat(), "period": nxt[1], "amount": inv.amount,
                        "cash_short": mode != "preview" and snap.cash + 0.005 < inv.amount,
                        "split": split(cents(amount), values, inv.weights)}
        put_in = self.journal.invest_put_in(mode) if mode != "preview" else 0.0
        return {
            "enabled": inv.enabled, "preview": mode == "preview", "mode": mode, "amount": inv.amount,
            "every": inv.every, "put_in": put_in, "value": sum(values.values()),
            "holdings": drift(values, inv.weights), "next": upcoming,
        }

    def summary_line(self, snap) -> str | None:
        if not self.inv.enabled:
            return None
        s = self.status(snap)
        nxt = f"Next: ${s['amount']:,.2f} on {date.fromisoformat(s['next']['date']):%b %-d}." if s["next"] else \
            "No more contributions scheduled."
        if s["preview"]:
            return f"Invest plan (preview, not buying): {nxt}"
        return f"Invest plan: ${s['put_in']:,.2f} put in, worth ${s['value']:,.2f} now. {nxt}"
