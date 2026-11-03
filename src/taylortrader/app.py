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
        dry_run=dry_run or cfg.dry_run,
        config_path=config_path,
        healthcheck_url=hc.get_secret_value() if hc else None,
    )
