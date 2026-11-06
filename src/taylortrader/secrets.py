"""Loading broker credentials without ever exposing them.

Rules:
- Secrets come from the environment, a git-ignored .env file, or the macOS Keychain. Never from config.
- Secret values are wrapped in pydantic SecretStr, so repr/str/logging show '**********'.
- Every loaded value is registered with the log redaction filter (see logging_setup.py).
- Error messages name the missing variable, never a value.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

KEYCHAIN_SERVICE = "taylortrader"
LIVE_CONFIRM_VAR = "TAYLORTRADER_ALLOW_LIVE"
# Set only from the command line on the Mac (`taylortrader wifi on`), never through the dashboard.
DASHBOARD_PASSCODE_VAR = "TAYLORTRADER_DASHBOARD_PASSCODE"
_STORABLE_ELSEWHERE = (LIVE_CONFIRM_VAR, DASHBOARD_PASSCODE_VAR)

# Every secret value loaded in this process. The logging filter masks these.
_LOADED_SECRETS: set[str] = set()


class SecretsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlpacaCredentials:
    key_id: SecretStr
    secret_key: SecretStr
    paper: bool

    def __repr__(self) -> str:  # belt and braces: never print values
        return f"AlpacaCredentials(paper={self.paper}, key_id=**********, secret_key=**********)"

    __str__ = __repr__


def loaded_secret_values() -> frozenset[str]:
    return frozenset(_LOADED_SECRETS)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values[name.strip()] = value
    return values


def _read_keychain(name: str, service: str = KEYCHAIN_SERVICE) -> str | None:
    if shutil.which("security") is None:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,  # we check returncode ourselves
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def get_secret(name: str, dotenv_path: Path | None = None, *, redact: bool = True) -> SecretStr | None:
    """Look up one secret: environment, then .env, then Keychain (service "taylortrader").
    `redact=False` for flags that aren't secret (masking "1" would garble every number)."""
    value = os.environ.get(name) or None
    if value is None:
        value = _read_dotenv(dotenv_path or Path(".env")).get(name)
    if value is None:
        value = _read_keychain(name)
    if value is None:
        return None
    if redact:
        _LOADED_SECRETS.add(value)
    return SecretStr(value)


def live_trading_confirmed(dotenv_path: Path | None = None) -> bool:
    """Live needs this flag in the environment, .env or Keychain. The dashboard sets it only
    after the typed go-live confirmation, and removes it when you switch back to paper."""
    return get_secret(LIVE_CONFIRM_VAR, dotenv_path, redact=False) is not None


def load_alpaca_credentials(mode: str, dotenv_path: Path | None = None) -> AlpacaCredentials:
    if mode not in ("paper", "live"):
        raise SecretsError(f"mode must be 'paper' or 'live', got {mode!r}")
    if mode == "live" and not live_trading_confirmed(dotenv_path):
        raise SecretsError(
            f"Config says live, but {LIVE_CONFIRM_VAR} is not set. Live trading needs both, on purpose."
        )
    prefix = "ALPACA_PAPER" if mode == "paper" else "ALPACA_LIVE"
    names = (f"{prefix}_KEY_ID", f"{prefix}_SECRET_KEY")
    key_id, secret_key = (get_secret(n, dotenv_path) for n in names)
    missing = [n for n, v in zip(names, (key_id, secret_key)) if v is None]
    if missing:
        raise SecretsError(
            f"Missing {', '.join(missing)}. Set it in the environment, .env, or the Keychain "
            f"(security add-generic-password -s {KEYCHAIN_SERVICE} -a <NAME> -w)."
        )
    return AlpacaCredentials(key_id=key_id, secret_key=secret_key, paper=(mode == "paper"))


# --- writing (used by the dashboard) -------------------------------------------------------------

# The only names the dashboard may write. Anything else is refused.
SETTABLE = (
    "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY",
    "NTFY_TOPIC", "EMAIL_PASSWORD", "HEALTHCHECK_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
)


def keychain_available() -> bool:
    return shutil.which("security") is not None


def _check_value(name: str, value: str) -> str:
    if name not in (*SETTABLE, *_STORABLE_ELSEWHERE):
        raise SecretsError(f"{name} isn't a setting TaylorTrader stores")
    value = value.strip()
    if not value or len(value) > 512 or any(ch in value for ch in "\r\n\0"):
        raise SecretsError(f"{name}: value is empty, too long, or has line breaks")
    return value


def _write_dotenv(path: Path, name: str, value: str | None) -> None:
    lines = path.read_text().splitlines() if path.is_file() else []
    lines = [ln for ln in lines if ln.partition("=")[0].strip() != name]
    if value is not None:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def set_secret(name: str, value: str, dotenv_path: Path | None = None) -> str:
    """Store a secret in the Keychain (macOS) or .env (elsewhere, mode 600). Returns where."""
    value = _check_value(name, value)
    if keychain_available():
        # The value is passed as an argument, so it's briefly visible to other processes run by
        # this same user. Acceptable on a single-user Mac; the Keychain is still where it lives.
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value],
            capture_output=True, timeout=10, check=False,
        )
        if r.returncode != 0:
            raise SecretsError(f"Keychain refused to store {name} (exit {r.returncode})")
        where = "Keychain"
    else:
        _write_dotenv(dotenv_path or Path(".env"), name, value)
        where = ".env"
    if name != LIVE_CONFIRM_VAR:
        _LOADED_SECRETS.add(value)
    return where


def delete_secret(name: str, dotenv_path: Path | None = None) -> None:
    if name not in (*SETTABLE, *_STORABLE_ELSEWHERE):
        raise SecretsError(f"{name} isn't a setting TaylorTrader stores")
    if keychain_available():
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
                       capture_output=True, timeout=10, check=False)
    path = dotenv_path or Path(".env")
    if path.is_file() and name in _read_dotenv(path):
        _write_dotenv(path, name, None)


def secret_source(name: str, dotenv_path: Path | None = None) -> str | None:
    """Where a secret is set ("environment", ".env", "Keychain"), or None. Never the value."""
    if os.environ.get(name):
        return "environment"
    if _read_dotenv(dotenv_path or Path(".env")).get(name):
        return ".env"
    if _read_keychain(name):
        return "Keychain"
    return None
