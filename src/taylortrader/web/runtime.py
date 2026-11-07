"""What the dashboard is looking at: settings, broker, data, journal. Rebuilt after every change.

The dashboard must work before anything is set up (that's how setup happens), so every
piece here may be missing, with a plain-English reason in place of it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from taylortrader.config import Config, ConfigError, load_config
from taylortrader.journal import Journal
from taylortrader.logging_setup import redact
from taylortrader.secrets import SecretsError

log = logging.getLogger(__name__)

# Defaults for a first save: dry run on paper, Yahoo data, no strategies until you pick one.
FIRST_RUN = Config(mode="paper", dry_run=True, feed="yahoo")


def _connect(cfg: Config, dotenv_path: Path | None):
    """Real wiring: Alpaca broker + market data + alert notes. Raises SecretsError without keys."""
    from taylortrader.app import build_alerts, build_market_data
    from taylortrader.broker import AlpacaBroker
    from taylortrader.secrets import load_alpaca_credentials

    creds = load_alpaca_credentials(cfg.mode, dotenv_path)
    return AlpacaBroker(creds), build_market_data(cfg, creds), build_alerts(cfg)[1]


@dataclass
class Runtime:
    config_path: Path
    dotenv_path: Path | None = None
    connect: Callable = _connect  # tests swap in fakes
    cfg: Config | None = None
    config_error: str | None = None
    broker: object = None
    market_data: object = None
    broker_error: str | None = None
    alert_notes: list[str] = field(default_factory=list)
    journal: Journal | None = None

    def __post_init__(self):
        self.reload()

    @property
    def configured(self) -> bool:
        return self.cfg is not None

    @property
    def settings(self) -> Config:
        """The current settings, or first-run defaults if there's no valid file yet."""
        return self.cfg or FIRST_RUN

    def reload(self) -> None:
        try:
            self.cfg, self.config_error = load_config(self.config_path), None
        except ConfigError as e:
            self.cfg, self.config_error = None, (None if not self.config_path.exists() else str(e))
        data_dir = self.settings.data_dir
        if self.journal is None or self.journal.path != data_dir / "journal.db":
            data_dir.mkdir(parents=True, exist_ok=True)
            self.journal = Journal(data_dir / "journal.db")
        self.broker = self.market_data = None
        self.broker_error, self.alert_notes = None, []
        if self.cfg is None:
            self.broker_error = "Not set up yet."
            return
        try:
            self.broker, self.market_data, self.alert_notes = self.connect(self.cfg, self.dotenv_path)
        except SecretsError as e:
            self.broker_error = str(e)
        except Exception as e:  # noqa: BLE001 - shown on the page, never crashes the dashboard
            log.warning("connect failed: %s", e)
            self.broker_error = redact(f"{type(e).__name__}: {e}")
