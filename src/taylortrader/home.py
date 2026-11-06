"""Where TaylorTrader keeps its settings, journal and logs, and how background jobs start it.

- In a source checkout (or any folder that already has a config.toml): that folder, as before.
- Installed as a tool (`uv tool install taylortrader`): ~/Library/Application Support/TaylorTrader on
  macOS, ~/.local/share/taylortrader elsewhere. Run from any folder; it always finds the same home.
- TAYLORTRADER_HOME overrides both.

Everything else uses paths relative to the home (config.toml, .env, var/), so the command line
changes into it once at startup.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HOME_VAR = "TAYLORTRADER_HOME"


def default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TaylorTrader"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "taylortrader"


def is_checkout(folder: Path) -> bool:
    py = folder / "pyproject.toml"
    try:
        return py.is_file() and 'name = "taylortrader"' in py.read_text()
    except OSError:
        return False


def find_home(cwd: Path | None = None) -> Path:
    if os.environ.get(HOME_VAR):
        return Path(os.environ[HOME_VAR]).expanduser()
    cwd = cwd or Path.cwd()
    if (cwd / "config.toml").exists() or is_checkout(cwd):
        return cwd
    return default_home()


def command(home: Path) -> list[str]:
    """How a background job runs `taylortrader`: the checkout's own environment, else the installed
    command, else `uv run` in the checkout. Never `uv run` when avoidable: uv doesn't pass launchd's
    SIGTERM on to Python, so stops would wait 20 seconds and end in a SIGKILL."""
    venv = home / ".venv" / "bin" / "taylortrader"
    if venv.exists():
        return [str(venv)]
    installed = shutil.which("taylortrader")
    if installed:
        return [installed]
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    return [uv, "run", "--project", str(home), "taylortrader"]
