"""The daily summary email: an HTML version for mail apps, and the same facts as plain text.

Email HTML is its own world: tables for layout, inline styles only, no web fonts, no scripts.
Everything that came from outside (symbols, reasons, error text) is escaped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DASHBOARD = "http://127.0.0.1:8787"

INK, INK2, INK3, RULE, SHEET, PAGE = "#16202a", "#4a5663", "#7a8591", "#e3e7eb", "#ffffff", "#f3f5f7"
UP, DOWN, WARN, INFO = "#18794a", "#c4262d", "#a15c00", "#2f5fa8"
# Dark mode, for mail apps that follow it (Apple Mail): each light colour gets a class, and a <style> block
# swaps in its dark twin. Mail apps that ignore the block (Gmail's app) show the light email.
DARK = {INK: "#e8edf2", INK2: "#b4bec9", INK3: "#8b96a3", UP: "#4cc38a", DOWN: "#ff6b6b", WARN: "#f0a640",
        INFO: "#7aa7ff"}
TEXT_CLASS = {INK: "tt-ink", INK2: "tt-ink2", INK3: "tt-ink3", UP: "tt-up", DOWN: "tt-down", WARN: "tt-warn",
              INFO: "tt-info"}
BG_CLASS = {PAGE: "tt-page", SHEET: "tt-sheet"}
DARK_CSS = (
    ":root{color-scheme:light dark;supported-color-schemes:light dark}"
    "@media (prefers-color-scheme:dark){"
    ".tt-page{background:#0f1419!important}"
    ".tt-sheet{background:#1a2129!important;border-color:#2b343e!important}"
    ".tt-tile{background:#232b35!important}"
    ".tt-rule{border-color:#2b343e!important}"
    + "".join(f".{c}{{color:{DARK[k]}!important}}" for k, c in TEXT_CLASS.items())
    + ".tt-light-img{display:none!important}"
    ".tt-dark-img{display:block!important;max-height:none!important;overflow:visible!important}}"
)
MODE_COLOR = {"LIVE": "#c4262d", "PAPER": "#2f5fa8", "DRY RUN": "#4a5663"}
# Long unbroken text (a URL, an error code) must wrap: otherwise phone mail apps zoom the whole email out.
WRAP = "word-break:break-word;overflow-wrap:anywhere"
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
    day_pnl: float | None = None  # dollars, equity now minus at the start of the day (money moved in/out excluded)
    closed_trades: list[dict] = field(default_factory=list)  # stats.Trade.as_dict() for trades closed today
    filled: dict | None = None  # {"buy": n, "sell": n} orders filled today at Alpaca (stop-losses included)
    charts: list = field(default_factory=list)  # emailchart.Chart: one per traded symbol, or the account's day

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


INVEST_PLAN = "invest plan"  # the owner name of invest plan holdings, which carry no stop on purpose


def _stop_words(p: PositionLine) -> str:
    return "yes" if p.has_stop else "not used (invest plan)" if p.owner == INVEST_PLAN else "NO"


def _stop_chip(p: PositionLine) -> str:
    if p.has_stop:
        return _chip("✓ At Alpaca", UP)
    return _chip("Not used", INK3) if p.owner == INVEST_PLAN else _chip("✕ None", DOWN)


def _gain_pct(p: PositionLine) -> float | None:
    if not p.avg or not p.qty:
        return None
    return (p.value / p.qty / p.avg - 1) * 100


def _today_words(s: Summary) -> str:
    """'+$12.34 (+1.20%) today', or just the percent when the dollar figure isn't known."""
    if s.day_pnl is None:
        return f"{s.day_pnl_pct:+.2f}% today"
    return f"{_signed_money(s.day_pnl)} ({s.day_pnl_pct:+.2f}%) today"


def _held(t: dict) -> str:
    try:
        mins = (datetime.fromisoformat(t["closed"]) - datetime.fromisoformat(t["opened"])).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        return ""
    if mins < 60:
        return f"{mins:.0f} min"
    if mins < 60 * 24:
        return f"{mins / 60:.1f} h"
    return f"{mins / 1440:.0f} d"


def _trade_who(t: dict) -> str:
    return t.get("strategy") or "not the bot's"


def _trades_line(s: Summary) -> str:
    """One line on today's activity: orders filled, trades closed, money made or lost on them."""
    bits = []
    if s.filled is not None:
        b, sl = s.filled.get("buy", 0), s.filled.get("sell", 0)
        bits.append(f"{b} buy{'' if b == 1 else 's'} and {sl} sell{'' if sl == 1 else 's'} filled")
    n = len(s.closed_trades)
    if n:
        won = sum(1 for t in s.closed_trades if t["pnl"] > 0)
        bits.append(f"{n} trade{'' if n == 1 else 's'} closed, {won} won, "
                    f"{_signed_money(sum(t['pnl'] for t in s.closed_trades))} realized")
    elif s.filled is not None:
        bits.append("no trades closed")
    return " · ".join(bits)


_JSON_ERROR = re.compile(r"\{[^{}]*\"message\"[^{}]*\}")


def plain(text: str) -> str:
    """Broker errors arrive as raw JSON ({"code":40310000,...,"message":"insufficient qty..."}): keep the message."""
    def one(m: re.Match) -> str:
        try:
            return str(json.loads(m.group(0))["message"])
        except (ValueError, KeyError, TypeError):
            return m.group(0)
    return _JSON_ERROR.sub(one, text or "")


# Connection drops and Alpaca server hiccups: the bot retries these by itself. One outage can log five
# errors at once, so they're grouped into one line and only counted as a warning when they last.
_NET = re.compile(r"ReadTimeout|ConnectTimeout|ConnectionError|SSLError|NameResolution|timed out|"
                  r"Timeout\b|RemoteDisconnected|Connection (?:aborted|reset|refused)", re.IGNORECASE)
_SERVER = re.compile(r"internal server error|service unavailable|bad gateway|gateway time-?out|"
                     r"too many requests|\"code\":\s*5\d{7}|<title>50[0234]\b", re.IGNORECASE)
_WHERE = re.compile(r"^(\w+) ([A-Z][A-Z0-9./]*): ")
BLIP_KINDS = ("error", "data")
BLIP_GAP = timedelta(minutes=10)   # errors closer than this belong to one outage
LONG_BLIP = timedelta(minutes=15)  # an outage this long is a real warning


def blip_kind(e: dict) -> str | None:
    """'server' (Alpaca's side), 'net' (the connection), or None: a problem the bot doesn't retry away."""
    if e.get("kind") not in BLIP_KINDS:
        return None
    m = e.get("message") or ""
    return "server" if _SERVER.search(m) else "net" if _NET.search(m) else None


@dataclass
class Blip:
    start: datetime
    end: datetime
    kinds: set[str]
    where: dict[str, list[str]]  # what was affected -> symbols
    count: int = 1

    @property
    def long(self) -> bool:
        return self.end - self.start >= LONG_BLIP

    def add(self, e: dict, t: datetime, kind: str) -> None:
        self.end, self.count = t, self.count + 1
        self.kinds.add(kind)
        _where(self.where, e)

    def title(self) -> str:
        return ("Alpaca server trouble" if self.kinds == {"server"} else
                "Connection dropped" if self.kinds == {"net"} else "Connection trouble")

    def when(self) -> str:
        a, b = et(self.start.isoformat()), et(self.end.isoformat())
        if a == b:
            return a
        sa, sb = a.rsplit(" ", 1), b.rsplit(" ", 1)
        return f"{sa[0]}–{b}" if sa[1] == sb[1] else f"{a}–{b}"

    def detail(self) -> str:
        mins = (self.end - self.start).total_seconds() / 60
        n = f"{self.count} error{'' if self.count == 1 else 's'}"
        span = f" over {mins:.0f} minutes" if mins >= 2 else ""
        where = ", ".join(f"{k} ({', '.join(v)})" if v else k for k, v in self.where.items())
        tail = "Check your internet and Alpaca's status page." if self.long else "The bot retried on its own."
        return f"{n}{span}: {where}. {tail}"


def _where(acc: dict[str, list[str]], e: dict) -> None:
    msg = e.get("message") or ""
    m = _WHERE.match(msg)
    if e.get("kind") == "data":
        key, sym = "market data", None
    elif m:
        key, sym = ("AI check" if m.group(1) == "ai" else m.group(1)), m.group(2)
    else:
        key, sym = "main loop", None
    syms = acc.setdefault(key, [])
    if sym and sym not in syms:
        syms.append(sym)


def group_problems(problems: list[dict]) -> list[dict | Blip]:
    """Problems in time order, with each run of connection errors folded into one Blip."""
    out: list[dict | Blip] = []
    cur: Blip | None = None
    for e in sorted(problems, key=lambda e: e.get("ts") or ""):
        kind = blip_kind(e)
        try:
            t = datetime.fromisoformat(e["ts"])
        except (KeyError, TypeError, ValueError):
            kind = None
        if kind is None:
            out.append(e)
            continue
        if cur is not None and t - cur.end <= BLIP_GAP:
            cur.add(e, t, kind)
        else:
            cur = Blip(t, t, {kind}, {})
            _where(cur.where, e)
            out.append(cur)
    return out


def warning_counts(s: Summary) -> tuple[int, int]:
    """(real warnings, short connection blips)."""
    items = group_problems(s.problems)
    blips = sum(1 for i in items if isinstance(i, Blip) and not i.long)
    return len(items) - blips, blips


def _warn_label(s: Summary) -> str:
    _, blips = warning_counts(s)
    return f"Warnings · {blips} blip{'' if blips == 1 else 's'}" if blips else "Warnings"


def _order_why(o: dict) -> str:
    why = o.get("reason") or ""
    if o.get("detail"):
        why += f" ({o['detail']})" if why else o["detail"]
    return plain(why)


# --- plain text ----------------------------------------------------------------------------------

def text(s: Summary) -> str:
    invested = sum(p.value for p in s.positions)
    lines = [
        (f"[{s.mode}] Equity {_money(s.equity)}, {_today_words(s)}, cash {_money(s.cash)}, "
         f"invested {invested / s.equity * 100 if s.equity else 0:.0f}%"),
        (f"New buys: {buys_status(s)[1]}. Orders today: {len(s.orders)}. "
         f"Problems today: {warning_counts(s)[0]}"
         + (f" (plus {warning_counts(s)[1]} connection blip(s) the bot rode out)." if warning_counts(s)[1] else ".")),
    ]
    if s.closed and s.closed.get("trades") and not s.closed_trades:  # the trades line below says it otherwise
        lines.append(f"Closed today: {s.closed['trades']} trade(s), {s.closed['wins']} won, "
                     f"{_signed_money(s.closed['realized'])}.")
    if _trades_line(s):
        lines += ["", f"Today's trades: {_trades_line(s)}."]
    for t in s.closed_trades:
        lines.append(f"  {t['symbol']}: {qty_text(t['qty'])} sh, ${t['entry']:,.2f} -> ${t['exit']:,.2f}, "
                     f"{_signed_money(t['pnl'])} ({t['pct']:+.1f}%), held {_held(t)}, {_trade_who(t)}")
    lines += ["", "Positions:" if s.positions else "Positions: none"]
    for p in s.positions:
        g = _gain_pct(p)
        lines.append(f"  {p.symbol}: {qty_text(p.qty)} sh, {_money(p.value)}"
                     f"{f' ({g:+.1f}%)' if g is not None else ''}, stop {_stop_words(p)}, "
                     f"{'managed by ' + p.owner if p.owner else 'not managed by the bot'}")
    if s.orders:
        lines += ["", "Orders (New York time):"]
        lines += [f"  {et(o['ts'])} {o['side']} {o['symbol']} {qty_text(o.get('qty'))} [{o['status']}] {_order_why(o)}"
                  for o in s.orders]
    if s.problems:
        lines += ["", "Problems:"]
        for i in group_problems(s.problems)[:20]:
            lines.append(f"  {i.when()} {i.title().lower()}: {i.detail()}" if isinstance(i, Blip)
                         else f"  {et(i['ts'])} {i['kind']}: {plain(i['message'])}")
    lines += ["", *s.notes, f"Dashboard (on your Mac): {DASHBOARD}",
              "If you stop getting this summary on trading days, TaylorTrader isn't running."]
    return "\n".join(lines)


# --- HTML ------------------------------------------------------------------------------------------

def _chip(label: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;'
            f'font-weight:600;color:{color};background:{color}1a;white-space:nowrap">{escape(label)}</span>')


def _stat(label: str, value: str, color: str = INK) -> str:
    return (f'<td class="tt-tile" style="padding:12px 14px;background:{PAGE};border-radius:10px;vertical-align:top">'
            f'<div style="font-size:12px;color:{INK3}">{escape(label)}</div>'
            f'<div style="font-size:18px;font-weight:600;color:{color};margin-top:3px;white-space:nowrap">{value}</div></td>')


def _chart(c, title: bool = True) -> str:
    """A light and a dark picture of the same chart; dark-mode mail apps swap them (see DARK_CSS)."""
    from taylortrader.emailchart import WIDTH_PX

    img = (f'<img src="cid:{{cid}}" width="{WIDTH_PX}" alt="{escape(c.alt)}" '
           f'style="display:block;width:100%;max-width:{WIDTH_PX}px;height:auto;border:0">')
    head = (f'<div style="font-size:13px;font-weight:600;color:{INK};margin:14px 0 4px">{escape(c.title)} today '
            f'<span style="font-weight:400;color:{INK3}">· ▲ buy ▼ sell</span></div>') if title else ""
    return (f'{head}<div class="tt-light-img">{img.replace("{cid}", c.cid + "-light")}</div>'
            f'<div class="tt-dark-img" style="display:none;max-height:0;overflow:hidden">'
            f'{img.replace("{cid}", c.cid + "-dark")}</div>')


def images(s: Summary) -> dict[str, bytes]:
    """The pictures the HTML email refers to, by content id."""
    out: dict[str, bytes] = {}
    for c in s.charts:
        out.update(c.images())
    return out


_TAG = re.compile(r'<([a-z]+)\b([^>]*?)\sstyle="([^"]*)"')
_COLOR = re.compile(r"(?<![-\w])color:(#[0-9a-f]{6})(?![0-9a-f])")
_BG = re.compile(r"(?<![-\w])background:(#[0-9a-f]{6})(?![0-9a-f])")


def _darkable(doc: str) -> str:
    """Give every element styled with one of the light colours the class that darkens it."""
    def tag(m: re.Match) -> str:
        if "class=" in m.group(2):
            return m.group(0)
        style, cls = m.group(3), []
        # the last one wins in CSS, so it's the one to darken (cells add a colour after the shared style)
        if (c := _COLOR.findall(style)) and c[-1] in TEXT_CLASS:
            cls.append(TEXT_CLASS[c[-1]])
        if (b := _BG.findall(style)) and b[-1] in BG_CLASS:
            cls.append(BG_CLASS[b[-1]])
        if "border" in style and RULE in style and "tt-sheet" not in cls:
            cls.append("tt-rule")
        if not cls:
            return m.group(0)
        return f'<{m.group(1)}{m.group(2)} class="{" ".join(cls)}" style="{style}"'
    return _TAG.sub(tag, doc)


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
        f'<div style="font-size:15px;font-weight:600;color:{pnl_color}">{arrow}{escape(_today_words(s))}</div>'
        f'</td></tr>'
    )]

    # stat tiles
    closed = s.closed or {}
    tiles = [
        _stat("Cash", _money(s.cash)),
        _stat("Invested", f"{invested / s.equity * 100 if s.equity else 0:.0f}%"),
        _stat("New buys", buys_status(s)[0], WARN if s.kill_on or s.loss_limit_hit else INK),
        _stat(_warn_label(s), str(warning_counts(s)[0]), DOWN if warning_counts(s)[0] else INK),
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

    # today's trades: what was bought and sold, and what each closed trade made or lost
    if _trades_line(s):
        body = f'<div style="font-size:13.5px;color:{INK2};margin-bottom:6px">{escape(_trades_line(s))}</div>'
        if s.closed_trades:
            body += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            for t in s.closed_trades:
                col = UP if t["pnl"] > 0 else DOWN if t["pnl"] < 0 else INK
                body += (f'<tr><td style="{td}"><b>{escape(t["symbol"])}</b>'
                         f'<div style="font-size:12px;color:{INK3}">{qty_text(t["qty"])} sh · '
                         f'${t["entry"]:,.2f} → ${t["exit"]:,.2f} · held {_held(t)} · {escape(_trade_who(t))}</div></td>'
                         f'<td style="{td};text-align:right;color:{col};white-space:nowrap"><b>{_signed_money(t["pnl"])}</b>'
                         f'<div style="font-size:12px">{t["pct"]:+.1f}%</div></td></tr>')
            body += "</table>"
        body += "".join(_chart(c) for c in s.charts if c.cid != "chart-account")
        rows.append(_section("Today's trades", body))
    account = [c for c in s.charts if c.cid == "chart-account"]
    if account:
        rows.append(_section(account[0].title, _chart(account[0], title=False)))

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
                     f'<td style="{td}">{_stop_chip(p)}</td></tr>')
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
                     f'<div style="font-size:12.5px;color:{INK2};margin-top:3px;{WRAP}">{escape(_order_why(o))}</div></td></tr>')
        body += "</table>"
        rows.append(_section("Orders", body))

    # problems
    if s.problems:
        body = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        items = group_problems(s.problems)
        for i in items[:20]:
            if isinstance(i, Blip):
                when, col, what, why = i.when(), (WARN if i.long else INK2), i.title(), i.detail()
            else:
                when, what, why = et(i["ts"]), i["kind"].replace("_", " "), plain(i["message"])
                col = DOWN if i["level"] == "error" else WARN
            body += (f'<tr><td style="{td};width:68px;color:{INK3}">{escape(when)}</td>'
                     f'<td style="{td}"><b style="color:{col}">{escape(what)}</b>'
                     f'<div style="font-size:12.5px;color:{INK2};margin-top:2px;{WRAP}">{escape(why)}</div></td></tr>')
        body += "</table>"
        real = warning_counts(s)[0]
        rows.append(_section("Warnings and errors" if real else "Connection blips", body))

    notes = "".join(f"<div>{escape(n)}</div>" for n in s.notes)
    rows.append(
        f'<tr><td style="padding:22px 24px 24px;font-size:12.5px;color:{INK3};line-height:1.5">{notes}'
        f'<div>Open the <a href="{DASHBOARD}" style="color:{INFO}">dashboard</a> on your Mac for details. '
        f'Times are New York time.</div>'
        f"<div>If you stop getting this summary on trading days, TaylorTrader isn't running.</div></td></tr>")

    return _darkable(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f'<style>{DARK_CSS}</style></head>'
        f'<body style="margin:0;padding:0;background:{PAGE}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}">'
        f'<tr><td align="center" style="padding:16px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;'
        f'background:{SHEET};border-radius:16px;border:1px solid {RULE};font-family:{font};color:{INK}">'
        f'<tr><td style="height:5px;background:{mode_color};border-radius:16px 16px 0 0;font-size:0;line-height:0">&nbsp;</td></tr>'
        + "".join(rows) +
        "</table></td></tr></table></body></html>"
    )
