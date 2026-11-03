"""Generate launchd agents for the service and the watchdog (macOS).

The plists contain paths only, never secrets: the service reads credentials from the Keychain.
"""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path

SERVICE = "com.taylortrader.service"
WATCHDOG = "com.taylortrader.watchdog"
AGENTS = Path.home() / "Library" / "LaunchAgents"


def plists(project: Path, dry_run: bool) -> dict[str, dict]:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    logs = project / "var" / "logs"
    base = [uv, "run", "--project", str(project), "taylortrader", "--config", str(project / "config.toml")]
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    return {
        SERVICE: {
            "Label": SERVICE,
            "ProgramArguments": [*base, "run", *(["--dry-run"] if dry_run else [])],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,          # restart whenever it exits
            "ThrottleInterval": 60,     # but not more than once a minute
            "StandardOutPath": str(logs / "service.out.log"),
            "StandardErrorPath": str(logs / "service.err.log"),
        },
        WATCHDOG: {
            "Label": WATCHDOG,
            "ProgramArguments": [*base, "watchdog"],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": env,
            "StartInterval": 600,       # every 10 minutes
            "StandardOutPath": str(logs / "watchdog.out.log"),
            "StandardErrorPath": str(logs / "watchdog.err.log"),
        },
    }


def write_plists(project: Path, dry_run: bool, dest: Path = AGENTS) -> list[Path]:
    (project / "var" / "logs").mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for label, data in plists(project, dry_run).items():
        path = dest / f"{label}.plist"
        with path.open("wb") as f:
            plistlib.dump(data, f)
        written.append(path)
    return written
