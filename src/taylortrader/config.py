"""Configuration: a TOML file with no secrets in it."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guardrails import Limits

TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")
# Setting names that look like they hold a secret value. "email_keychain_service" is fine;
# "api_key", "secret_key", "ntfy_token", "password" are not.
_SECRET_NAME = re.compile(r"(?i)(secret|password|passwd|token|(^|_)key$|(^|_)key_id$|api_key|topic$|url$)")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    symbols: list[str]
    timeframe: str = "1Day"
    allocation_pct: float = 10.0
    stop_loss_pct: float = 8.0
    take_profit_pct: float = 0.0
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertsConfig:
    ntfy_server: str = "https://ntfy.sh"
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)
    email_keychain_service: str = ""  # optional extra Keychain service to find the email password
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587


@dataclass(frozen=True)
class ServiceConfig:
    loop_seconds: int = 30
    heartbeat_stale_minutes: int = 15
    daily_summary: bool = True
    auto_protect: bool = True  # re-create a missing stop on a position a strategy owns


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    data_dir: Path = Path("var")
    feed: str = "iex"
    strategies: list[StrategyConfig] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)

    @property
    def enabled_strategies(self) -> list[StrategyConfig]:
        return [s for s in self.strategies if s.enabled]


def _reject_secrets(table: dict[str, Any], where: str = "") -> None:
    for key, value in table.items():
        path = f"{where}{key}"
        if isinstance(value, dict):
            _reject_secrets(value, path + ".")
        elif _SECRET_NAME.search(key):
            raise ConfigError(
                f"'{path}' looks like a secret. Secrets don't belong in config: use .env or the Keychain."
            )


def load_config(path: Path | str = "config.toml") -> Config:
    from taylortrader.strategies import get_strategy  # avoid import cycle

    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path} not found. Copy config.example.toml to {path.name} and edit it.")
    raw = tomllib.loads(path.read_text())
    _reject_secrets(raw)

    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError(f"mode must be 'paper' or 'live', got {mode!r}")
    feed = raw.get("feed", "iex")
    if feed not in ("iex", "sip"):
        raise ConfigError(f"feed must be 'iex' or 'sip', got {feed!r}")

    strategies = []
    for i, block in enumerate(raw.get("strategies", [])):
        try:
            sc = StrategyConfig(**block)
        except TypeError as e:
            raise ConfigError(f"strategies[{i}]: {e}") from None
        if sc.timeframe not in TIMEFRAMES:
            raise ConfigError(f"strategies[{i}] ({sc.name}): timeframe must be one of {TIMEFRAMES}")
        if not sc.symbols:
            raise ConfigError(f"strategies[{i}] ({sc.name}): needs at least one symbol")
        get_strategy(sc.name, sc.params)  # validates name and params now, not at 9:30am
        strategies.append(sc)

    owners: dict[str, str] = {}
    for sc in strategies:
        if not sc.enabled:
            continue
        for sym in sc.symbols:
            if sym in owners:
                raise ConfigError(
                    f"{sym} is used by both {owners[sym]} and {sc.name}. One strategy owns each symbol, "
                    "so it's always clear who manages a position."
                )
            owners[sym] = sc.name

    def _section(cls, name):
        try:
            return cls(**raw.get(name, {}))
        except TypeError as e:
            raise ConfigError(f"[{name}]: {e}") from None

    return Config(
        mode=mode,
        data_dir=Path(raw.get("data_dir", "var")),
        feed=feed,
        strategies=strategies,
        limits=Limits.from_config(raw.get("guardrails")),
        alerts=_section(AlertsConfig, "alerts"),
        service=_section(ServiceConfig, "service"),
    )
