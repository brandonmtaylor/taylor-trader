"""The daily email's extras: connection blips grouped, dark mode, and trade charts sent inside the email."""
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from taylortrader import emailchart, report
from taylortrader.alerts import EmailNotifier

NET = "ReadTimeout: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Read timed out. (read timeout=5.0)"


def ev(ts, message, kind="error", level="error"):
    return {"ts": ts, "level": level, "kind": kind, "message": message}


def summary(**kw):
    base = {"day": date(2026, 1, 7), "mode": "PAPER", "equity": 1000.0, "day_pnl_pct": 0.5, "day_pnl": 5.0, "cash": 1000.0,
                "kill_on": False, "positions": [], "orders": [], "problems": []}
    return report.Summary(**{**base, **kw})


def test_one_outage_is_one_line_and_not_a_warning():
    probs = [ev("2026-01-07T20:16:26+00:00", f"ai {s}: ConnectionError: Max retries (NameResolutionError)")
             for s in ("NVDA", "AMZN")] + [ev("2026-01-07T20:17:26+00:00", NET)]
    items = report.group_problems(probs)
    assert len(items) == 1 and isinstance(items[0], report.Blip)
    b = items[0]
    assert b.count == 3 and not b.long and b.when() == "3:16–3:17 PM"
    assert "AI check (NVDA, AMZN), main loop" in b.detail()
    s = summary(problems=probs)
    assert report.warning_counts(s) == (0, 1)
    page = report.html(s)
    assert "Warnings · 1 blip" in page and "Connection blips" in page and "Connection dropped" in page


def test_real_errors_still_count_and_long_outages_warn():
    held = ev("2026-01-07T15:00:00+00:00", 'macd_rsi VRT: APIError: {"code":40310000,"message":"insufficient qty"}')
    auth = ev("2026-01-07T15:01:00+00:00", "APIError: <html><title>401 Authorization Required</title></html>")
    outage = [ev(f"2026-01-07T16:{m:02d}:00+00:00", NET) for m in range(0, 25, 5)]  # 20 minutes, 5 min apart
    s = summary(problems=[held, auth, *outage])
    assert report.warning_counts(s) == (3, 0)
    assert "Warnings and errors" in report.html(s)
    assert "Check your internet" in report.text(s)


def test_alpaca_server_errors_and_rate_limits_are_blips():
    assert report.blip_kind(ev("t", 'APIError: {"code":50010000,"message":"internal server error occurred"}')) == "server"
    assert report.blip_kind(ev("t", 'ai NVDA: APIError: {"message": "too many requests."}')) == "server"
    assert report.blip_kind(ev("t", "yahoo data failed (Timeout: curl: (28))", kind="data", level="warning")) == "net"
    assert report.blip_kind(ev("t", NET, kind="kill_switch")) is None


def test_dark_mode_classes_follow_the_colour_that_shows():
    page = report.html(summary(closed={"trades": 1, "wins": 1, "realized": 6.83}, closed_trades=[
        {"symbol": "VRT", "qty": 1, "entry": 187.17, "exit": 194.0, "pnl": 6.83, "pct": 3.6,
         "opened": "2026-01-06T17:00:00+00:00", "closed": "2026-01-07T14:30:00+00:00", "strategy": "macd_rsi"}]))
    assert "prefers-color-scheme:dark" in page and 'content="light dark"' in page
    assert 'class="tt-up tt-rule"' in page   # the gain cell: green, not the row's default ink
    assert 'class="tt-page"' in page and 'tt-sheet"' in page and 'class="tt-tile"' in page


def _bars():
    idx = pd.date_range("2026-01-07 09:30", "2026-01-07 15:55", freq="5min", tz="America/New_York").tz_convert("UTC")
    return pd.DataFrame({"close": 100 + np.cumsum(np.random.default_rng(1).normal(0, .3, len(idx)))}, index=idx)


def test_trade_chart_draws_light_and_dark_pngs():
    c = emailchart.trade_chart("BRK.B", _bars(), [emailchart.Mark(datetime(2026, 1, 7, 15, 0, tzinfo=UTC), 100.5, "buy")],
                               (98.0, "Bought $98.00 on Jan 6"))
    assert c.cid == "chart-brk-b" and "bought at $100.50" in c.alt
    assert all(v.startswith(b"\x89PNG") for v in c.images().values())
    assert emailchart.trade_chart("X", pd.DataFrame(), []) is None


def test_flat_account_day_draws_no_chart():
    t = [datetime(2026, 1, 7, 15, m, tzinfo=UTC) for m in (0, 5, 10)]
    assert emailchart.equity_chart(list(zip(t, [500.0] * 3)), 500.0) is None
    assert emailchart.equity_chart(list(zip(t, [500.0, 503.0, 501.0])), 500.0).cid == "chart-account"


def test_charts_travel_inside_the_email(monkeypatch):
    c = emailchart.Chart("chart-vrt", "VRT", "VRT price today", b"\x89PNG-light", b"\x89PNG-dark")
    s = summary(charts=[c], filled={"buy": 0, "sell": 1})
    page = report.html(s)
    assert 'src="cid:chart-vrt-light"' in page and 'src="cid:chart-vrt-dark"' in page
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr("taylortrader.alerts.smtplib.SMTP", FakeSMTP)
    from pydantic import SecretStr
    EmailNotifier("bot@example.com", SecretStr("x"), ["me@example.com"]).send(
        "Daily summary", report.text(s), html=page, images=report.images(s))
    parts = {p.get("Content-ID"): p.get_content() for p in sent[0].walk() if p.get_content_maintype() == "image"}
    assert parts == {"<chart-vrt-light>": b"\x89PNG-light", "<chart-vrt-dark>": b"\x89PNG-dark"}
