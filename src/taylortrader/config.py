"""Configuration: a TOML file with no secrets in it."""

from __future__ import annotations

import math
import re
import shutil
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
    no_trades_days: int = 14  # note a strategy that holds nothing and hasn't traded this long; 0 turns it off
    auto_protect: bool = True  # re-create a missing stop on a position a strategy owns


@dataclass(frozen=True)
class AIConfig:
    """The model AI strategies ask. API keys live in the Keychain/.env, never here."""
    provider: str = "anthropic"       # "anthropic" (Claude API), "openai" (any OpenAI-compatible API),
                                      # or "claude-code" (Claude Code CLI on your subscription)
    model: str = "claude-opus-5-5"
    endpoint: str = ""                # OpenAI-compatible only, e.g. http://localhost:11434/v1 for Ollama
    effort: str = "medium"            # Claude only: low, medium, high, xhigh, max
    max_calls_per_day: int = 40       # across all AI strategies; then they hold until tomorrow
    timeout_seconds: int = 120


INVEST_EVERY = ("week", "2weeks", "month", "once")
MAX_HOLDINGS = 20


@dataclass(frozen=True)
class InvestConfig:
    """A long-term invest plan: put `amount` into these weights on a schedule. No stops, no signals."""
    enabled: bool = False
    preview: bool = True        # run the schedule and alert what it would buy, but place no orders
    amount: float = 0.0         # dollars per contribution
    every: str = "month"        # week | 2weeks | month | once (all at once)
    day: int = 1                # month: day 1-28; week/2weeks: 0=Monday .. 4=Friday
    start: str = ""             # YYYY-MM-DD: nothing before this ("" = as soon as it's enabled)
    until: str = ""             # YYYY-MM-DD: nothing after this ("" = keep going)
    reserve: bool = True        # keep one contribution out of what trading strategies size from
    holdings: list[dict[str, Any]] = field(default_factory=list)  # [{symbol, weight}], weights sum to 100

    @property
    def symbols(self) -> list[str]:
        return [h["symbol"] for h in self.holdings]

    @property
    def weights(self) -> dict[str, float]:
        return {h["symbol"]: float(h["weight"]) for h in self.holdings}


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
    invest: InvestConfig = field(default_factory=InvestConfig)

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


def _number(label: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{label}: {name} must be a number, got {value!r}")
    return float(value)


def parse_config(raw: dict[str, Any]) -> Config:
    """Validate a TOML-shaped dict (from the file, or from the dashboard) into a Config."""
    try:
        return _parse_config(raw)
    except ConfigError:
        raise
    except (TypeError, ValueError, AttributeError) as e:
        # A wrongly typed value in a hand-edited file (an unquoted date, a number where a list goes)
        # is a settings mistake to report, not a crash.
        raise ConfigError(f"a setting has the wrong type: {e}") from None


def _parse_config(raw: dict[str, Any]) -> Config:
    from guardrails.limits import LimitsError
    from taylortrader.strategies import get_strategy  # avoid import cycle

    _reject_secrets(raw)
    known = {"mode", "dry_run", "data_dir", "feed", "strategies", "guardrails", "alerts", "service", "ai", "invest"}
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
        if not isinstance(block.get("symbols", []), list):
            raise ConfigError(f'{label}: symbols must be a list, like ["SPY"]')
        for name in ("allocation_pct", "stop_loss_pct", "take_profit_pct"):
            if name in block:
                _number(label, name, block[name])
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
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$", str(ai.model)):
        raise ConfigError("[ai] model must be a model name like claude-opus-5-5, sonnet or llama3.1")
    if ai.endpoint and not re.match(r"^https?://[^\s]+$", ai.endpoint):
        raise ConfigError("[ai] endpoint must be an http(s) address")
    if not 1 <= int(ai.max_calls_per_day) <= 2000:
        raise ConfigError("[ai] max_calls_per_day must be between 1 and 2000")
    if not 10 <= int(ai.timeout_seconds) <= 600:
        raise ConfigError("[ai] timeout_seconds must be between 10 and 600")

    invest = parse_invest(raw.get("invest", {}), owners)
    alerts = _section(AlertsConfig, "alerts")
    if not re.match(r"^https?://[^\s]+$", alerts.ntfy_server):
        raise ConfigError("[alerts] ntfy_server must be an http(s) address")
    if not re.match(r"^[A-Za-z0-9.\-]{1,253}$", alerts.smtp_host):
        raise ConfigError("[alerts] smtp_host must be a host name")
    if isinstance(alerts.smtp_port, bool) or not isinstance(alerts.smtp_port, int) or not 1 <= alerts.smtp_port <= 65535:
        raise ConfigError("[alerts] smtp_port must be a port number")
    if not isinstance(alerts.email_to, list) or not all(isinstance(a, str) and "@" in a for a in alerts.email_to):
        raise ConfigError('[alerts] email_to must be a list of addresses, like ["you@example.com"]')
    service = _section(ServiceConfig, "service")
    if not 5 <= _number("[service]", "loop_seconds", service.loop_seconds) <= 3600:
        raise ConfigError("[service] loop_seconds must be between 5 and 3600")
    if not 1 <= _number("[service]", "heartbeat_stale_minutes", service.heartbeat_stale_minutes) <= 1440:
        raise ConfigError("[service] heartbeat_stale_minutes must be between 1 and 1440")
    if not 0 <= _number("[service]", "no_trades_days", service.no_trades_days) <= 365:
        raise ConfigError("[service] no_trades_days must be between 0 (off) and 365")

    return Config(
        mode=mode,
        dry_run=dry_run,
        data_dir=Path(raw.get("data_dir", "var")),
        feed=feed,
        strategies=strategies,
        limits=limits,
        alerts=alerts,
        service=service,
        ai=ai,
        invest=invest,
    )


def parse_invest(block: dict[str, Any], owners: dict[str, str]) -> InvestConfig:
    from datetime import date

    from guardrails.invest import HARD_MAX_CONTRIBUTION

    try:
        inv = InvestConfig(**block)
    except TypeError as e:
        raise ConfigError(f"[invest]: {e}") from None
    holdings = []
    for h in inv.holdings:
        if not isinstance(h, dict) or set(h) != {"symbol", "weight"}:
            raise ConfigError("[invest] each holding needs exactly a symbol and a weight")
        sym = str(h["symbol"]).strip().upper()
        if not _SYMBOL.match(sym) or "/" in sym:
            raise ConfigError(f"[invest] {sym!r} doesn't look like a stock or ETF symbol")
        try:
            w = float(h["weight"])
        except (TypeError, ValueError):
            raise ConfigError(f"[invest] {sym}: weight must be a number") from None
        if not (math.isfinite(w) and 0 < w <= 100):
            raise ConfigError(f"[invest] {sym}: weight must be between 0 and 100")
        holdings.append({"symbol": sym, "weight": w})
    inv = replace(inv, holdings=holdings)
    syms = inv.symbols
    if len(set(syms)) != len(syms):
        raise ConfigError("[invest] a symbol is listed twice")
    if len(syms) > MAX_HOLDINGS:
        raise ConfigError(f"[invest] at most {MAX_HOLDINGS} holdings")
    for sym in syms:
        # Alpaca keeps one position per symbol, so two owners couldn't be told apart.
        if sym in owners:
            raise ConfigError(f"{sym} is in the invest plan and in {owners[sym]}. Each symbol has one owner.")
    if syms and abs(sum(inv.weights.values()) - 100) > 0.01:
        raise ConfigError(f"[invest] weights add up to {sum(inv.weights.values()):g}%, not 100%")
    if inv.every not in INVEST_EVERY:
        raise ConfigError(f"[invest] every must be one of {INVEST_EVERY}")
    if inv.every == "month" and not 1 <= int(inv.day) <= 28:
        raise ConfigError("[invest] day must be 1-28 for monthly (every month has those days)")
    if inv.every in ("week", "2weeks") and not 0 <= int(inv.day) <= 4:
        raise ConfigError("[invest] day must be 0 (Monday) to 4 (Friday) for weekly")
    dates = {}
    for name in ("start", "until"):
        v = getattr(inv, name)
        if v:
            try:
                dates[name] = date.fromisoformat(v)
            except ValueError:
                raise ConfigError(f"[invest] {name} must be a date like 2026-12-01") from None
    if len(dates) == 2 and dates["until"] < dates["start"]:
        raise ConfigError("[invest] until is before start")
    if not isinstance(inv.amount, int | float) or not math.isfinite(inv.amount) or inv.amount < 0:
        raise ConfigError("[invest] amount must be a positive number of dollars")
    if inv.amount > HARD_MAX_CONTRIBUTION:
        raise ConfigError(f"[invest] amount is over the ${HARD_MAX_CONTRIBUTION:,.0f} limit per contribution")
    for name in ("enabled", "preview", "reserve"):
        if not isinstance(getattr(inv, name), bool):
            raise ConfigError(f"[invest] {name} must be true or false")
    if inv.enabled:
        if not syms:
            raise ConfigError("[invest] add at least one holding before turning the plan on")
        if inv.amount < 1:
            raise ConfigError("[invest] amount must be at least $1 (Alpaca's smallest order)")
    return inv


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
        "invest": asdict(cfg.invest),
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
    if isinstance(v, dict):  # inline table, e.g. a holding
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
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
    inv = data.get("invest")
    if inv and (inv.get("enabled") or inv.get("holdings")):  # don't add an empty section to every file
        rest = {k: v for k, v in inv.items() if k != "holdings"}
        lines += ["", "[invest]", *_toml_table(rest), "holdings = ["]
        lines += [f"  {_toml_value(h)}," for h in inv.get("holdings", [])]
        lines += ["]"]
    return "\n".join(lines) + "\n"


def save_config(cfg_or_raw: Config | dict[str, Any], path: Path | str = "config.toml") -> Config:
    """Validate, then write atomically. The previous file is kept as <name>.bak."""
    raw = config_to_dict(cfg_or_raw) if isinstance(cfg_or_raw, Config) else cfg_or_raw
    cfg = parse_config(raw)  # raises ConfigError before anything is written
    path = Path(path)
    text = render_toml(config_to_dict(cfg), path.name)
    try:
        if parse_config(tomllib.loads(text)) != cfg:  # the writer must round-trip exactly
            raise ConfigError("internal error: settings didn't survive being written; nothing was saved")
    except tomllib.TOMLDecodeError:
        raise ConfigError("a setting contains characters that can't be saved (like a control character)") from None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))  # a copy, so config.toml never goes missing
    tmp.replace(path)
    return cfg
