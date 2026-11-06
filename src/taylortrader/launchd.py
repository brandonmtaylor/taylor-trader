"""Generate launchd agents for the service, the watchdog and the dashboard (macOS).

The dashboard has its own agent so it's always at http://127.0.0.1:8787, comes back after a crash or
login, and can be restarted after an update without anyone finding the terminal it ran in. Starting
or stopping the bot from the dashboard never touches the dashboard's own agent.

The plists contain paths only, never secrets: the service reads credentials from the Keychain.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from taylortrader.home import command

SERVICE = "com.taylortrader.service"
WATCHDOG = "com.taylortrader.watchdog"
DASHBOARD = "com.taylortrader.dashboard"
AGENTS = Path.home() / "Library" / "LaunchAgents"


def plists(project: Path) -> dict[str, dict]:
    """Dry run, paper and live are settings in config.toml, not service arguments."""
    logs = project / "var" / "logs"
    base = [*command(project), "--config", str(project / "config.toml")]
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    return {
        SERVICE: {
            "Label": SERVICE,
            "ProgramArguments": [*base, "run"],
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
        DASHBOARD: {
            "Label": DASHBOARD,
            "ProgramArguments": [*base, "dashboard", "--no-open"],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / "dashboard.out.log"),
            "StandardErrorPath": str(logs / "dashboard.err.log"),
        },
    }


def write_plists(project: Path, dest: Path = AGENTS) -> list[Path]:
    (project / "var" / "logs").mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for label, data in plists(project).items():
        path = dest / f"{label}.plist"
        with path.open("wb") as f:
            plistlib.dump(data, f)
        written.append(path)
    return written


# --- control (used by the dashboard) -------------------------------------------------------------

def supported() -> bool:
    return shutil.which("launchctl") is not None


def _domain() -> str:
    import os

    return f"gui/{os.getuid()}"


def _launchctl(*args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=timeout, check=False)


def status(label: str = SERVICE, dest: Path = AGENTS) -> dict:
    """{"supported", "installed", "loaded", "running", "pid"} for one agent."""
    out = {"supported": supported(), "installed": (dest / f"{label}.plist").exists(),
           "loaded": False, "running": False, "pid": None}
    if not out["supported"]:
        return out
    r = _launchctl("print", f"{_domain()}/{label}")
    if r.returncode == 0:
        out["loaded"] = True
        for line in r.stdout.splitlines():
            line = line.strip()
            if line == "state = running":
                out["running"] = True
            elif line.startswith("pid = ") and line[6:].isdigit():
                out["pid"] = int(line[6:])
    return out


def start(project: Path, dest: Path = AGENTS) -> None:
    """Write the agents (always current) and load them. Starting twice is harmless."""
    write_plists(project, dest)
    for label in (SERVICE, WATCHDOG):
        if not status(label, dest)["loaded"]:
            r = _launchctl("bootstrap", _domain(), str(dest / f"{label}.plist"))
            if r.returncode != 0:
                raise RuntimeError(f"launchctl couldn't start {label}: {r.stderr.strip() or r.returncode}")


def stop(dest: Path = AGENTS) -> None:
    for label in (SERVICE, WATCHDOG):
        if status(label, dest)["loaded"]:
            _launchctl("bootout", f"{_domain()}/{label}")


def restart() -> None:
    _launchctl("kickstart", "-k", f"{_domain()}/{SERVICE}")


def start_dashboard(project: Path, dest: Path = AGENTS) -> None:
    """Run the dashboard in the background from now on (and at every login)."""
    write_plists(project, dest)
    if not status(DASHBOARD, dest)["loaded"]:
        r = _launchctl("bootstrap", _domain(), str(dest / f"{DASHBOARD}.plist"))
        if r.returncode != 0:
            raise RuntimeError(f"launchctl couldn't start the dashboard: {r.stderr.strip() or r.returncode}")


def stop_dashboard(dest: Path = AGENTS) -> None:
    if status(DASHBOARD, dest)["loaded"]:
        _launchctl("bootout", f"{_domain()}/{DASHBOARD}")


def restart_dashboard() -> None:
    _launchctl("kickstart", "-k", f"{_domain()}/{DASHBOARD}", timeout=60)
