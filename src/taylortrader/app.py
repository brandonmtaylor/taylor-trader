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


def build_service(cfg: Config, dry_run: bool = False):
    from taylortrader.broker import AlpacaBroker
    from taylortrader.data import MarketData
    from taylortrader.service import Service

    creds = load_alpaca_credentials(cfg.mode)
    alerts, notes = build_alerts(cfg)
    for n in notes:
        log.info("alerts %s", n)
    hc = get_secret("HEALTHCHECK_URL")
    return Service(
        cfg=cfg,
        broker=AlpacaBroker(creds),
        market_data=MarketData(creds, cfg.data_dir / "cache", feed=cfg.feed),
        journal=open_journal(cfg),
        alerts=alerts,
        dry_run=dry_run,
        healthcheck_url=hc.get_secret_value() if hc else None,
    )
