"""Small PNG charts for the daily email.

Mail apps run no scripts and Gmail drops inline SVG, so a chart in an email has to be a picture, sent
inside the email (a cid: image). Each chart is drawn twice, light and dark: mail apps that follow dark
mode (Apple Mail) show the dark one, the rest the light one.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")

THEMES = {
    "light": {"bg": "#ffffff", "ink": "#4a5663", "grid": "#e3e7eb", "line": "#2f5fa8",
              "up": "#18794a", "down": "#c4262d", "ref": "#7a8591"},
    "dark": {"bg": "#1a2129", "ink": "#b4bec9", "grid": "#2b343e", "line": "#7aa7ff",
             "up": "#4cc38a", "down": "#ff6b6b", "ref": "#8b96a3"},
}
WIDTH_PX = 552   # the email's content width
W, H, DPI = 5.52, 2.3, 200  # inches at 200 dpi: sharp on phone screens, ~35 KB each
FONT = 9.5  # points; shown at 552 px wide this reads like the email's 13 px text


@dataclass
class Chart:
    cid: str      # the email holds f"{cid}-light" and f"{cid}-dark"
    title: str
    alt: str
    light: bytes
    dark: bytes

    def images(self) -> dict[str, bytes]:
        return {f"{self.cid}-light": self.light, f"{self.cid}-dark": self.dark}


@dataclass
class Mark:
    ts: datetime
    price: float
    side: str  # buy | sell


def _ny(ts) -> datetime:
    return pd.Timestamp(ts).tz_convert(NY).tz_localize(None).to_pydatetime()


def _render(x: list[datetime], y: list[float], marks: list[Mark], ref: tuple[float, str] | None,
            theme: dict, money: bool = True) -> bytes:
    import matplotlib

    matplotlib.use("Agg")  # no window: the service runs in the background
    from matplotlib.dates import DateFormatter, HourLocator
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    fig = Figure(figsize=(W, H), dpi=DPI, facecolor=theme["bg"])
    ax = fig.add_axes((0.02, 0.14, 0.82, 0.82), facecolor=theme["bg"])
    ax.plot(x, y, color=theme["line"], linewidth=1.6, solid_joinstyle="round", zorder=2)
    ys = list(y) + [m.price for m in marks] + ([ref[0]] if ref else [])
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.14 or max(abs(hi) * 0.002, 0.01)
    ax.set_ylim(lo - pad, hi + pad)
    xs = list(x) + [_ny(m.ts) for m in marks]
    span = max(xs) - min(xs)
    ax.set_xlim(min(xs) - span * 0.03, max(xs) + span * 0.03)  # room for a marker at the open or close
    if ref:
        ax.axhline(ref[0], color=theme["ref"], linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.annotate(ref[1], (0, ref[0]), xycoords=("axes fraction", "data"), xytext=(4, 4),
                    textcoords="offset points", fontsize=FONT, color=theme["ink"], va="bottom")
    for m in marks:
        buy = m.side == "buy"
        ax.scatter([_ny(m.ts)], [m.price], marker="^" if buy else "v", s=90, zorder=4,
                   color=theme["up"] if buy else theme["down"], edgecolors=theme["bg"], linewidths=1.4)
        ax.annotate(f"{'Bought' if buy else 'Sold'} ${m.price:,.2f}", (_ny(m.ts), m.price),
                    xytext=(7, -11 if buy else 7), textcoords="offset points", fontsize=FONT,
                    color=theme["ink"], va="center", fontweight="bold")
    ax.yaxis.tick_right()
    fmt = (lambda v, _: f"${v:,.0f}" if hi - lo > 20 else f"${v:,.2f}") if money else (lambda v, _: f"{v:,.2f}")
    ax.yaxis.set_major_formatter(FuncFormatter(fmt))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3))
    ax.xaxis.set_major_locator(HourLocator(byhour=[10, 12, 14, 16]))
    ax.xaxis.set_major_formatter(DateFormatter("%-I %p"))
    ax.grid(axis="y", color=theme["grid"], linewidth=0.8, zorder=0)
    ax.tick_params(colors=theme["ink"], labelsize=FONT, length=0, pad=4)
    for s in ax.spines.values():
        s.set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=theme["bg"])
    return buf.getvalue()


def _both(x, y, marks, ref, money=True) -> tuple[bytes, bytes]:
    return tuple(_render(x, y, marks, ref, THEMES[t], money) for t in ("light", "dark"))  # type: ignore[return-value]


def trade_chart(symbol: str, bars: pd.DataFrame, marks: list[Mark], entry: tuple[float, str] | None = None) -> Chart | None:
    """The day's price with a marker on each buy and sell. `entry`: (price, label) for a position bought
    on an earlier day and sold today, drawn as a dashed line so the gain or loss reads at a glance."""
    if bars is None or bars.empty or "close" not in bars:
        return None
    x = [_ny(t) for t in bars.index]
    light, dark = _both(x, bars["close"].astype(float).tolist(), marks, entry)
    acts = ", ".join(f"{'bought' if m.side == 'buy' else 'sold'} at ${m.price:,.2f}" for m in marks)
    return Chart(f"chart-{symbol.lower().replace('/', '-').replace('.', '-')}", symbol,
                 f"{symbol} price today{': ' + acts if acts else ''}", light, dark)


def equity_chart(points: list[tuple[datetime, float]], start: float | None) -> Chart | None:
    """The account's value through the day, against where it started (dashed)."""
    if len(points) < 2:
        return None
    x, y = [_ny(t) for t, _ in points], [v for _, v in points]
    if max(y) - min(y) < 0.01:
        return None  # a flat line (all cash) says nothing
    ref = (start, f"Start ${start:,.2f}") if start else None
    light, dark = _both(x, y, [], ref)
    return Chart("chart-account", "Your account today", f"Account value today, from ${y[0]:,.2f} to ${y[-1]:,.2f}",
                 light, dark)
