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


def get_secret(name: str, dotenv_path: Path | None = None) -> SecretStr | None:
    """Look up one secret: environment, then .env, then Keychain (service "taylortrader")."""
    value = os.environ.get(name) or None
    if value is None:
        value = _read_dotenv(dotenv_path or Path(".env")).get(name)
    if value is None:
        value = _read_keychain(name)
    if value is None:
        return None
    _LOADED_SECRETS.add(value)
    return SecretStr(value)


def live_trading_confirmed() -> bool:
    return bool(os.environ.get(LIVE_CONFIRM_VAR, "").strip())


def load_alpaca_credentials(mode: str, dotenv_path: Path | None = None) -> AlpacaCredentials:
    if mode not in ("paper", "live"):
        raise SecretsError(f"mode must be 'paper' or 'live', got {mode!r}")
    if mode == "live" and not live_trading_confirmed():
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
