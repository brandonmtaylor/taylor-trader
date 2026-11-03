"""Configuration: a TOML file with no secrets in it."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from guardrails import Limits

TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")
FEEDS = ("yahoo", "iex", "sip")
# Setting names that look like they hold a secret value ("api_key", "secret_key", "ntfy_topic",
# "password", "healthcheck_url"). Those belong in the environment, .env or the Keychain.
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
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587


@dataclass(frozen=True)
class ServiceConfig:
    loop_seconds: int = 30
    heartbeat_stale_minutes: int = 15
    daily_summary: bool = True
    auto_protect: bool = True  # re-create a missing stop on a position a strategy owns


@dataclass(frozen=True)
class AIConfig:
    """The model AI strategies ask. API keys live in the Keychain/.env, never here."""
    provider: str = "anthropic"       # "anthropic" (Claude) or "openai" (any OpenAI-compatible API)
    model: str = "claude-opus-5-5"
    endpoint: str = ""                # OpenAI-compatible only, e.g. http://localhost:11434/v1 for Ollama
    effort: str = "medium"            # Claude only: low, medium, high, xhigh, max
    max_calls_per_day: int = 40       # across all AI strategies; then they hold until tomorrow
    timeout_seconds: int = 120


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    dry_run: bool = False  # decide and alert, but place no orders
    data_dir: Path = Path("var")
    feed: str = "iex"
    strategies: list[StrategyConfig] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    ai: AIConfig = field(default_factory=AIConfig)

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
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path} not found. Run `taylortrader dashboard` to set up, or copy config.example.toml.")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(raw)


_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,14}$")


def parse_config(raw: dict[str, Any]) -> Config:
    """Validate a TOML-shaped dict (from the file, or from the dashboard) into a Config."""
    from guardrails.limits import LimitsError
    from taylortrader.strategies import get_strategy  # avoid import cycle

    _reject_secrets(raw)
    known = {"mode", "dry_run", "data_dir", "feed", "strategies", "guardrails", "alerts", "service", "ai"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown setting(s): {sorted(unknown)}")

    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError(f"mode must be 'paper' or 'live', got {mode!r}")
    dry_run = raw.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ConfigError("dry_run must be true or false")
    feed = raw.get("feed", "iex")
    if feed not in FEEDS:
        raise ConfigError(f"feed must be one of {FEEDS}, got {feed!r}")
    try:
        limits = Limits.from_config(raw.get("guardrails"))
    except LimitsError as e:
        raise ConfigError(f"guardrails: {e}") from None

    strategies = []
    for i, block in enumerate(raw.get("strategies", [])):
        label = f"strategy {i + 1} ({block.get('name', '?')})"
        try:
            sc = StrategyConfig(**block)
        except TypeError as e:
            raise ConfigError(f"{label}: {e}") from None
        sc = replace(sc, symbols=[str(x).strip().upper() for x in sc.symbols if str(x).strip()])
        if sc.timeframe not in TIMEFRAMES:
            raise ConfigError(f"{label}: timeframe must be one of {TIMEFRAMES}")
        if not sc.symbols:
            raise ConfigError(f"{label}: needs at least one symbol")
        bad = [x for x in sc.symbols if not _SYMBOL.match(x)]
        if bad:
            raise ConfigError(f"{label}: {', '.join(bad)} doesn't look like a ticker symbol")
        if len(set(sc.symbols)) != len(sc.symbols):
            raise ConfigError(f"{label}: a symbol is listed twice")
        if not 0 < sc.allocation_pct <= limits.max_position_pct:
            raise ConfigError(f"{label}: position size must be between 0 and {limits.max_position_pct}% "
                              "(the one-position guardrail)")
        if not 0 < sc.stop_loss_pct <= limits.max_stop_loss_pct:
            raise ConfigError(f"{label}: stop-loss must be between 0 and {limits.max_stop_loss_pct}% "
                              "(the stop-width guardrail)")
        if sc.take_profit_pct < 0:
            raise ConfigError(f"{label}: take-profit can't be negative (0 turns it off)")
        try:
            get_strategy(sc.name, sc.params)  # validates name and params now, not at 9:30am
        except ValueError as e:
            raise ConfigError(f"{label}: {e}") from None
        strategies.append(sc)

    owners: dict[str, str] = {}
    for i, sc in enumerate(strategies):
        if not sc.enabled:
            continue
        for sym in sc.symbols:
            if sym in owners:
                raise ConfigError(
                    f"{sym} is used by both {owners[sym]} and strategy {i + 1} ({sc.name}). One strategy owns "
                    "each symbol, so it's always clear who manages a position."
                )
            owners[sym] = f"strategy {i + 1} ({sc.name})"

    def _section(cls, name):
        try:
            return cls(**raw.get(name, {}))
        except TypeError as e:
            raise ConfigError(f"[{name}]: {e}") from None

    from taylortrader.llm import EFFORTS, PROVIDERS

    ai = _section(AIConfig, "ai")
    if ai.provider not in PROVIDERS:
        raise ConfigError(f"[ai] provider must be one of {PROVIDERS}")
    if ai.effort not in EFFORTS:
        raise ConfigError(f"[ai] effort must be one of {EFFORTS}")
    if not str(ai.model).strip():
        raise ConfigError("[ai] model can't be empty")
    if ai.endpoint and not re.match(r"^https?://[^\s]+$", ai.endpoint):
        raise ConfigError("[ai] endpoint must be an http(s) address")
    if not 1 <= int(ai.max_calls_per_day) <= 2000:
        raise ConfigError("[ai] max_calls_per_day must be between 1 and 2000")
    if not 10 <= int(ai.timeout_seconds) <= 600:
        raise ConfigError("[ai] timeout_seconds must be between 10 and 600")

    return Config(
        mode=mode,
        dry_run=dry_run,
        data_dir=Path(raw.get("data_dir", "var")),
        feed=feed,
        strategies=strategies,
        limits=limits,
        alerts=_section(AlertsConfig, "alerts"),
        service=_section(ServiceConfig, "service"),
        ai=ai,
    )


# --- writing ------------------------------------------------------------------------------------

HEADER = """\
# TaylorTrader settings. The dashboard writes this file; editing it by hand is fine too
# (config.example.toml explains every setting). No secrets go here: keys and passwords
# live in the Keychain or .env. The previous version is kept as {name}.bak.
"""


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """The TOML-shaped dict for a Config (round-trips through parse_config)."""
    return {
        "mode": cfg.mode,
        "dry_run": cfg.dry_run,
        "data_dir": str(cfg.data_dir),
        "feed": cfg.feed,
        "strategies": [asdict(sc) for sc in cfg.strategies],
        "guardrails": asdict(cfg.limits),
        "alerts": asdict(cfg.alerts),
        "service": asdict(cfg.service),
        "ai": asdict(cfg.ai),
    }


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        if not math.isfinite(v):
            raise ConfigError("numbers must be finite")
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ConfigError(f"can't write {type(v).__name__} to config")


def _toml_table(d: dict[str, Any]) -> list[str]:
    return [f"{k} = {_toml_value(v)}" for k, v in d.items() if not isinstance(v, dict)]


def render_toml(data: dict[str, Any], name: str = "config.toml") -> str:
    lines = [HEADER.format(name=name)]
    lines += _toml_table({k: v for k, v in data.items() if k not in ("strategies",)})
    for sc in data.get("strategies", []):
        lines += ["", "[[strategies]]", *_toml_table({k: v for k, v in sc.items() if k != "params"})]
        if sc.get("params"):
            lines += ["[strategies.params]", *_toml_table(sc["params"])]
    for section in ("guardrails", "alerts", "service", "ai"):
        if data.get(section):
            lines += ["", f"[{section}]", *_toml_table(data[section])]
    return "\n".join(lines) + "\n"


def save_config(cfg_or_raw: Config | dict[str, Any], path: Path | str = "config.toml") -> Config:
    """Validate, then write atomically. The previous file is kept as <name>.bak."""
    raw = config_to_dict(cfg_or_raw) if isinstance(cfg_or_raw, Config) else cfg_or_raw
    cfg = parse_config(raw)  # raises ConfigError before anything is written
    path = Path(path)
    text = render_toml(config_to_dict(cfg), path.name)
    if parse_config(tomllib.loads(text)) != cfg:  # the writer must round-trip exactly
        raise ConfigError("internal error: settings didn't survive being written; nothing was saved")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    if path.exists():
        path.replace(path.with_name(path.name + ".bak"))
    tmp.replace(path)
    return cfg
