"""Wiring: build alerts and the service from config + secrets."""

from __future__ import annotations

import logging

from taylortrader.alerts import Alerts, EmailNotifier, NtfyNotifier
from taylortrader.config import Config
from taylortrader.journal import Journal
from taylortrader.secrets import get_secret, load_alpaca_credentials

log = logging.getLogger(__name__)


def build_alerts(cfg: Config) -> tuple[Alerts, list[str]]:
    """Returns the alert router and a list of human-readable channel notes (no secret values)."""
    alerts, notes = Alerts(), []
    topic = get_secret("NTFY_TOPIC")
    if topic:
        alerts.push.append(NtfyNotifier(topic, cfg.alerts.ntfy_server))
        notes.append(f"ntfy: on ({cfg.alerts.ntfy_server})")
    else:
        notes.append("ntfy: OFF (set NTFY_TOPIC in the Keychain or .env)")
    a = cfg.alerts
    if a.email_from and a.email_to:
        password = get_secret("EMAIL_PASSWORD")
        if password:
            alerts.email.append(EmailNotifier(a.email_from, password, a.email_to, a.smtp_host, a.smtp_port))
            notes.append(f"email: on ({len(a.email_to)} recipient(s))")
        else:
            notes.append("email: OFF (set EMAIL_PASSWORD in the Keychain or .env)")
    else:
        notes.append("email: OFF (set email_from and email_to in [alerts])")
    return alerts, notes


def open_journal(cfg: Config) -> Journal:
    return Journal(cfg.data_dir / "journal.db")


def build_market_data(cfg: Config, creds, journal=None):
    """The configured feed. "yahoo" is wrapped with an automatic fallback to Alpaca IEX
    (IEX, not SIP: the free SIP feed holds back the newest 15 minutes, too stale to trade on)."""
    from taylortrader.data import FallbackData, MarketData, YahooData

    cache = cfg.data_dir / "cache"
    if cfg.feed != "yahoo":
        return MarketData(creds, cache, feed=cfg.feed)

    def on_switch(message: str, level: str) -> None:
        getattr(log, level)("data feed: %s", message)
        if journal is not None:
            journal.event("data", message, level)

    return FallbackData(YahooData(), MarketData(creds, cache, feed="iex"), on_switch=on_switch)


def build_news(creds):
    """Recent headlines for a symbol from Alpaca (free), cached 10 minutes per symbol."""
    import time
    from datetime import UTC, datetime, timedelta

    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(creds.key_id.get_secret_value(), creds.secret_key.get_secret_value())
    cache: dict[str, tuple[float, list[dict]]] = {}

    def fetch(symbol: str) -> list[dict]:
        hit = cache.get(symbol)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        res = client.get_news(NewsRequest(symbols=symbol, start=datetime.now(UTC) - timedelta(days=3),
                                          limit=8, sort="desc"))
        items = [{"time": n.created_at.strftime("%m-%d %H:%M UTC"), "source": n.source,
                  "headline": n.headline[:300], "summary": (n.summary or "")[:400]}
                 for n in res.data.get("news", [])]
        cache[symbol] = (time.monotonic(), items)
        return items

    return fetch


def build_ai(cfg: Config, creds, journal):
    """The AI strategies' context, or None if no AI strategy is enabled. Never raises: a missing
    key or package becomes llm_error, and AI strategies hold (and say why) until it's fixed."""
    if not any(sc.name == "ai" for sc in cfg.enabled_strategies):
        return None
    from taylortrader import llm
    from taylortrader.strategies.ai import AIContext

    a = cfg.ai
    ctx = AIContext(journal=journal, max_calls_per_day=a.max_calls_per_day, news=build_news(creds))
    try:
        ctx.llm = llm.build(a.provider, a.model, a.endpoint, a.effort, a.timeout_seconds)
    except Exception as e:  # noqa: BLE001 - reported through the strategy, never fatal
        ctx.llm_error = str(e)
    log.info("ai: %s %s%s", a.provider, a.model, f" (not ready: {ctx.llm_error})" if ctx.llm_error else "")
    return ctx


def build_service(cfg: Config, dry_run: bool = False, config_path=None):
    from taylortrader.broker import AlpacaBroker
    from taylortrader.service import Service

    creds = load_alpaca_credentials(cfg.mode)
    alerts, notes = build_alerts(cfg)
    for n in notes:
        log.info("alerts %s", n)
    hc = get_secret("HEALTHCHECK_URL")
    journal = open_journal(cfg)
    return Service(
        cfg=cfg,
        broker=AlpacaBroker(creds),
        market_data=build_market_data(cfg, creds, journal),
        journal=journal,
        alerts=alerts,
        ai=build_ai(cfg, creds, journal),
        dry_run=dry_run or cfg.dry_run,
        config_path=config_path,
        healthcheck_url=hc.get_secret_value() if hc else None,
    )
