"""The daily summary email: an HTML version for mail apps, and the same facts as plain text.

Email HTML is its own world: tables for layout, inline styles only, no web fonts, no scripts.
Everything that came from outside (symbols, reasons, error text) is escaped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DASHBOARD = "http://127.0.0.1:8787"

# Colours chosen to read on both light and (Gmail/iOS auto-inverted) dark backgrounds.
INK, INK2, INK3, RULE, SHEET, PAGE = "#16202a", "#4a5663", "#7a8591", "#e3e7eb", "#ffffff", "#f3f5f7"
UP, DOWN, WARN, INFO = "#18794a", "#c4262d", "#a15c00", "#2f5fa8"
MODE_COLOR = {"LIVE": "#c4262d", "PAPER": "#2f5fa8", "DRY RUN": "#4a5663"}
STATUS_COLOR = {"filled": UP, "submitted": INFO, "dry-run": INFO, "blocked": WARN, "failed": DOWN,
                "canceled": WARN, "expired": WARN, "rejected": DOWN}


@dataclass
class PositionLine:
    symbol: str
    qty: float
    value: float
    avg: float
    has_stop: bool
    owner: str | None


@dataclass
class Summary:
    day: date
    mode: str                     # LIVE | PAPER | DRY RUN
    equity: float
    day_pnl_pct: float
    cash: float
    kill_on: bool
    positions: list[PositionLine]
    orders: list[dict]            # journal rows
    problems: list[dict]          # journal events (error/warning)
    closed: dict | None = None    # stats.summarize()["today"], when available
    notes: list[str] = field(default_factory=list)
    loss_limit_pct: float | None = None  # the daily loss guardrail, to say when it blocked buys today

    @property
    def loss_limit_hit(self) -> bool:
        return self.loss_limit_pct is not None and self.day_pnl_pct <= -self.loss_limit_pct


def buys_status(s: Summary) -> tuple[str, str]:
    """(short, long) wording for whether the bot may open new positions."""
    if s.kill_on:
        return "Paused", "PAUSED (kill switch on)"
    if s.loss_limit_hit:
        return ("Blocked today",
                (f"blocked today (down {abs(s.day_pnl_pct):.1f}%, daily limit {s.loss_limit_pct:g}%); allowed again "
                "tomorrow"))
    return "Allowed", "allowed"


def qty_text(q: float | None) -> str:
    if q is None:
        return ""
    if float(q).is_integer():
        return f"{int(q):,}"
    return f"{q:.8f}".rstrip("0").rstrip(".")  # never 4.175e-05


def et(ts: str) -> str:
    """Journal timestamps are UTC ISO; people read New York time."""
    try:
        return datetime.fromisoformat(ts).astimezone(NY).strftime("%-I:%M %p")
    except (TypeError, ValueError):
        return ts or ""


def _money(n: float) -> str:
    return f"-${abs(n):,.2f}" if n < 0 else f"${n:,.2f}"


def _signed_money(n: float) -> str:
    return f"+${n:,.2f}" if n > 0 else _money(n)


def _gain_pct(p: PositionLine) -> float | None:
    if not p.avg or not p.qty:
        return None
    return (p.value / p.qty / p.avg - 1) * 100


def _order_why(o: dict) -> str:
    why = o.get("reason") or ""
    if o.get("detail"):
        why += f" ({o['detail']})" if why else o["detail"]
    return why


# --- plain text ----------------------------------------------------------------------------------

def text(s: Summary) -> str:
    invested = sum(p.value for p in s.positions)
    lines = [
        (f"[{s.mode}] Equity {_money(s.equity)} ({s.day_pnl_pct:+.2f}% today), cash {_money(s.cash)}, "
         f"invested {invested / s.equity * 100 if s.equity else 0:.0f}%"),
        (f"New buys: {buys_status(s)[1]}. Orders today: {len(s.orders)}. "
         f"Problems today: {len(s.problems)}."),
    ]
    if s.closed and s.closed.get("trades"):
        lines.append(f"Closed today: {s.closed['trades']} trade(s), {s.closed['wins']} won, "
                     f"{_signed_money(s.closed['realized'])}.")
    lines += ["", "Positions:" if s.positions else "Positions: none"]
    for p in s.positions:
        g = _gain_pct(p)
        lines.append(f"  {p.symbol}: {qty_text(p.qty)} sh, {_money(p.value)}"
                     f"{f' ({g:+.1f}%)' if g is not None else ''}, stop {'yes' if p.has_stop else 'NO'}, "
                     f"{'managed by ' + p.owner if p.owner else 'not managed by the bot'}")
    if s.orders:
        lines += ["", "Orders (New York time):"]
        lines += [f"  {et(o['ts'])} {o['side']} {o['symbol']} {qty_text(o.get('qty'))} [{o['status']}] {_order_why(o)}"
                  for o in s.orders]
    if s.problems:
        lines += ["", "Problems:"] + [f"  {et(e['ts'])} {e['kind']}: {e['message']}" for e in s.problems[:20]]
    lines += ["", *s.notes, f"Dashboard (on your Mac): {DASHBOARD}",
              "If you stop getting this summary on trading days, TaylorTrader isn't running."]
    return "\n".join(lines)


# --- HTML ------------------------------------------------------------------------------------------

def _chip(label: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;'
            f'font-weight:600;color:{color};background:{color}1a;white-space:nowrap">{escape(label)}</span>')


def _stat(label: str, value: str, color: str = INK) -> str:
    return (f'<td style="padding:12px 14px;background:{PAGE};border-radius:10px;vertical-align:top">'
            f'<div style="font-size:12px;color:{INK3}">{escape(label)}</div>'
            f'<div style="font-size:18px;font-weight:600;color:{color};margin-top:3px;white-space:nowrap">{value}</div></td>')


def _section(title: str, body: str) -> str:
    return (f'<tr><td style="padding:22px 24px 0">'
            f'<div style="font-size:15px;font-weight:700;color:{INK};margin-bottom:10px">{escape(title)}</div>'
            f'{body}</td></tr>')


def html(s: Summary) -> str:
    invested = sum(p.value for p in s.positions)
    pnl_color = UP if s.day_pnl_pct > 0 else DOWN if s.day_pnl_pct < 0 else INK2
    arrow = "▲ " if s.day_pnl_pct > 0 else "▼ " if s.day_pnl_pct < 0 else ""
    mode_color = MODE_COLOR.get(s.mode, INK2)
    day = s.day.strftime("%A, %B %-d")
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    td = f"padding:9px 8px;border-top:1px solid {RULE};font-size:13.5px;color:{INK};vertical-align:top"
    th = f"padding:0 8px 6px;font-size:11px;font-weight:600;color:{INK3};text-transform:uppercase;letter-spacing:.04em"

    # headline
    rows = [(
        f'<tr><td style="padding:22px 24px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-size:14px;font-weight:700;color:{INK}">TaylorTrader</td>'
        f'<td align="right">{_chip(s.mode, mode_color)}</td></tr></table>'
        f'<div style="font-size:13px;color:{INK3};margin-top:14px">{escape(day)} · after the close</div>'
        f'<div style="font-size:34px;font-weight:700;color:{INK};letter-spacing:-.02em;margin-top:2px">{_money(s.equity)}</div>'
        f'<div style="font-size:15px;font-weight:600;color:{pnl_color}">{arrow}{s.day_pnl_pct:+.2f}% today</div>'
        f'</td></tr>'
    )]

    # stat tiles
    closed = s.closed or {}
    tiles = [
        _stat("Cash", _money(s.cash)),
        _stat("Invested", f"{invested / s.equity * 100 if s.equity else 0:.0f}%"),
        _stat("New buys", buys_status(s)[0], WARN if s.kill_on or s.loss_limit_hit else INK),
        _stat("Warnings", str(len(s.problems)), DOWN if s.problems else INK),
    ]
    if closed.get("trades"):
        r = closed["realized"]
        tiles.insert(2, _stat(f"Closed · {closed['wins']}/{closed['trades']} won", _signed_money(r),
                              UP if r > 0 else DOWN if r < 0 else INK))
    # Two per row: four across overflows a phone, and email can't rely on media queries.
    spacer = '<td style="width:8px"></td>'
    pairs = [tiles[i:i + 2] for i in range(0, len(tiles), 2)]
    grid = '<tr><td colspan="3" style="height:8px;font-size:0;line-height:0">&nbsp;</td></tr>'.join(
        f'<tr>{spacer.join(p) if len(p) == 2 else p[0] + spacer + "<td></td>"}</tr>' for p in pairs)
    rows.append(f'<tr><td style="padding:18px 24px 0"><table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0" style="table-layout:fixed">{grid}</table></td></tr>')

    # positions
    if s.positions:
        body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="{th}">Symbol</td><td style="{th};text-align:right">Value</td>'
                f'<td style="{th};text-align:right">Gain</td><td style="{th}">Stop-loss</td></tr>')
        for p in s.positions:
            g = _gain_pct(p)
            gtxt = "—" if g is None else f"{g:+.1f}%"
            gcol = INK3 if g is None else UP if g > 0 else DOWN if g < 0 else INK
            owner = escape(p.owner) if p.owner else "not the bot's"
            body += (f'<tr><td style="{td}"><b>{escape(p.symbol)}</b>'
                     f'<div style="font-size:12px;color:{INK3}">{qty_text(p.qty)} sh · {owner}</div></td>'
                     f'<td style="{td};text-align:right;white-space:nowrap">{_money(p.value)}</td>'
                     f'<td style="{td};text-align:right;color:{gcol};white-space:nowrap">{gtxt}</td>'
                     f'<td style="{td}">{_chip("✓ At Alpaca", UP) if p.has_stop else _chip("✕ None", DOWN)}</td></tr>')
        body += "</table>"
    else:
        body = f'<div style="font-size:14px;color:{INK2}">Nothing held.</div>'
    rows.append(_section("Positions", body))

    # orders
    if s.orders:
        body = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        for o in s.orders:
            side_col = UP if o["side"] == "buy" else DOWN
            q = qty_text(o.get("qty"))
            body += (f'<tr><td style="{td};width:68px;color:{INK3};white-space:nowrap">{et(o["ts"])}</td>'
                     f'<td style="{td}"><b style="color:{side_col};text-transform:uppercase;font-size:12px">'
                     f'{escape(o["side"])}</b> <b>{escape(o["symbol"])}</b>'
                     f'{f" · {q} sh" if q and q != "0" else ""} '
                     f'{_chip(o["status"], STATUS_COLOR.get(o["status"], INFO))}'
                     f'<div style="font-size:12.5px;color:{INK2};margin-top:3px">{escape(_order_why(o))}</div></td></tr>')
        body += "</table>"
        rows.append(_section("Orders", body))

    # problems
    if s.problems:
        body = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        for e in s.problems[:20]:
            col = DOWN if e["level"] == "error" else WARN
            body += (f'<tr><td style="{td};width:68px;color:{INK3};white-space:nowrap">{et(e["ts"])}</td>'
                     f'<td style="{td}"><b style="color:{col}">{escape(e["kind"].replace("_", " "))}</b>'
                     f'<div style="font-size:12.5px;color:{INK2};margin-top:2px">{escape(e["message"])}</div></td></tr>')
        body += "</table>"
        rows.append(_section("Warnings and errors", body))

    notes = "".join(f"<div>{escape(n)}</div>" for n in s.notes)
    rows.append(
        f'<tr><td style="padding:22px 24px 24px;font-size:12.5px;color:{INK3};line-height:1.5">{notes}'
        f'<div>Open the <a href="{DASHBOARD}" style="color:{INFO}">dashboard</a> on your Mac for details. '
        f'Times are New York time.</div>'
        f"<div>If you stop getting this summary on trading days, TaylorTrader isn't running.</div></td></tr>")

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"></head>'
        f'<body style="margin:0;padding:0;background:{PAGE}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}">'
        f'<tr><td align="center" style="padding:16px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;'
        f'background:{SHEET};border-radius:16px;border:1px solid {RULE};font-family:{font};color:{INK}">'
        f'<tr><td style="height:5px;background:{mode_color};border-radius:16px 16px 0 0;font-size:0;line-height:0">&nbsp;</td></tr>'
        + "".join(rows) +
        "</table></td></tr></table></body></html>"
    )
