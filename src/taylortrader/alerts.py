"""Alerts: ntfy push notifications for anything urgent, email for the daily summary.

The ntfy topic is treated as a secret: on the public ntfy.sh server, anyone who knows a topic
name can read it. It comes from NTFY_TOPIC (env, .env or Keychain), never from config.
"""

from __future__ import annotations

import logging
import smtplib
import time
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import IntEnum

from pydantic import SecretStr

from taylortrader.logging_setup import redact

log = logging.getLogger(__name__)


class Level(IntEnum):
    LOW = 2       # ntfy low: listed in the app, no sound or buzz
    INFO = 3      # ntfy default priority
    WARNING = 4   # high
    CRITICAL = 5  # max: breaks through Do Not Disturb if you allow it in the app


_NTFY_TAGS = {Level.LOW: "gear", Level.INFO: "chart_with_upwards_trend", Level.WARNING: "warning", Level.CRITICAL: "rotating_light"}


class Notifier:
    def send(self, title: str, message: str, level: Level = Level.INFO) -> None:
        raise NotImplementedError


class NtfyNotifier(Notifier):
    def __init__(self, topic: SecretStr, server: str = "https://ntfy.sh", timeout: float = 10):
        self._topic = topic
        self.server = server.rstrip("/")
        self.timeout = timeout

    def send(self, title: str, message: str, level: Level = Level.INFO) -> None:
        url = f"{self.server}/{self._topic.get_secret_value()}"
        req = urllib.request.Request(
            url,
            data=message.encode(),
            method="POST",
            headers={"Title": title, "Priority": str(int(level)), "Tags": _NTFY_TAGS[level]},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


class EmailNotifier(Notifier):
    def __init__(self, sender: str, password: SecretStr, recipients: list[str],
                 host: str = "smtp.gmail.com", port: int = 587):
        self.sender, self._password, self.recipients = sender, password, recipients
        self.host, self.port = host, port

    def send(self, title: str, message: str, level: Level = Level.INFO, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[TaylorTrader] {title}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(message)  # plain text: what mail apps without HTML show
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            s.starttls()
            s.login(self.sender, self._password.get_secret_value())
            s.send_message(msg)


@dataclass
class Alerts:
    """Routes alerts, redacts them, and rate-limits repeats so a stuck loop can't spam your phone.

    A failing channel never raises into the trading loop; it's logged instead.
    """

    push: list[Notifier] = field(default_factory=list)   # urgent + routine (ntfy)
    email: list[Notifier] = field(default_factory=list)  # daily summary, critical copies
    cooldown_s: float = 30 * 60
    _last: dict[str, float] = field(default_factory=dict)

    def send(self, title: str, message: str = "", level: Level = Level.INFO,
             key: str | None = None, email_copy: bool = False) -> bool:
        key = key or title
        now = time.monotonic()
        if key in self._last and now - self._last[key] < self.cooldown_s:
            log.debug("alert %r suppressed (cooldown)", key)
            return False
        self._last[key] = now
        title, message = redact(title), redact(message)
        log.log(logging.WARNING if level >= Level.WARNING else logging.INFO, "ALERT %s: %s", title, message)
        targets = list(self.push)
        if email_copy or level is Level.CRITICAL:
            targets += self.email
        for n in targets:
            try:
                n.send(title, message, level)
            except Exception as e:  # noqa: BLE001  an alert channel must never take down trading
                log.error("alert via %s failed: %s", type(n).__name__, e)
        return True

    def summary(self, title: str, message: str, html: str | None = None) -> None:
        """Daily summary: email (HTML with a plain-text fallback), plus a short push."""
        for n in self.email:
            try:
                if html and isinstance(n, EmailNotifier):
                    n.send(title, redact(message), Level.INFO, html=redact(html))
                else:
                    n.send(title, redact(message), Level.INFO)
            except Exception as e:  # noqa: BLE001
                log.error("summary email failed: %s", e)
        first_lines = "\n".join(message.splitlines()[:6])
        self.send(title, first_lines, Level.INFO, key=f"summary:{title}")
