"""Opening the dashboard to other devices on your Wi-Fi, behind a passcode.

Off by default. `taylortrader wifi on` makes a passcode, keeps it in the Keychain, and restarts the
dashboard listening on the network; `taylortrader wifi off` removes it and goes back to this Mac only.

How it's protected, when it's on:
- This Mac (127.0.0.1) needs no passcode, same as before.
- Any other device signs in once with the passcode and gets a cookie good for 30 days. The cookie
  is signed with a key made from the passcode, so changing the passcode signs every device out.
- Wrong guesses are limited: 5 per device and 20 in total per 15 minutes, then sign-ins wait.
- Like the planned phone app: going live and API keys stay on the Mac. Other devices can see
  everything, pause, sell, and change settings, but can't switch to real money or touch keys.
- Host names: localhost, IP addresses (which can't be DNS-rebound), and this Mac's own .local name.

Not protected: it's plain http, so someone already on your Wi-Fi who can intercept traffic could
read the passcode. Fine for a home network you trust; use Tailscale for anywhere else.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets as pysecrets
import socket
import subprocess
import time
from collections import defaultdict, deque

from taylortrader.secrets import DASHBOARD_PASSCODE_VAR as PASSCODE_VAR  # noqa: F401 - re-exported

COOKIE = "tt_session"
SESSION_DAYS = 30
WINDOW_S = 15 * 60
PER_DEVICE_TRIES = 5
TOTAL_TRIES = 20
# No 0/o, 1/l/i: easy to read off a terminal and type on a phone.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def new_passcode() -> str:
    """Three groups of four, about 59 bits: far beyond what 20 guesses per 15 minutes can reach."""
    return "-".join("".join(pysecrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def _norm(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def is_loopback(client_host: str | None) -> bool:
    try:
        return client_host is not None and ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


def mac_name() -> str | None:
    """This Mac's Bonjour name, e.g. "Brandons-Mac-mini" (reachable as Brandons-Mac-mini.local)."""
    try:
        r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5, check=False)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def lan_address() -> str | None:
    """The address other devices on the network use. Connecting a UDP socket sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: picks the outbound interface, no traffic
            return s.getsockname()[0]
    except OSError:
        return None


def host_allowed(host_header: str, lan: bool, name: str | None) -> bool:
    host = host_header.lower()
    if host.startswith("["):  # [v6]:port
        host = host[: host.find("]") + 1]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if host in LOCAL_HOSTS:
        return True
    if not lan:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return name is not None and host == f"{name.lower()}.local"


class Sessions:
    """Stateless signed cookies, so a dashboard restart doesn't sign the phone out."""

    def __init__(self, passcode: str, clock=time.time):
        self._code = _norm(passcode)
        self._key = hashlib.sha256(b"taylortrader-session\0" + self._code.encode()).digest()
        self._clock = clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._all: deque[float] = deque()

    def _sig(self, expires: int) -> str:
        return hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(self._clock()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sig(expires)}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        exp, _, sig = cookie.partition(".")
        if not exp.isdigit() or int(exp) < self._clock():
            return False
        return hmac.compare_digest(sig, self._sig(int(exp)))

    def _trim(self, q: deque[float]) -> None:
        cutoff = self._clock() - WINDOW_S
        while q and q[0] < cutoff:
            q.popleft()

    def wait_s(self, device: str) -> int:
        """Seconds until this device may try again; 0 if it may try now."""
        q = self._fails[device]
        self._trim(q)
        self._trim(self._all)
        blocking = [x for x, n in ((q, PER_DEVICE_TRIES), (self._all, TOTAL_TRIES)) if len(x) >= n]
        if not blocking:
            return 0
        return max(int(x[0] + WINDOW_S - self._clock()) + 1 for x in blocking)

    def check(self, device: str, passcode: str) -> bool:
        ok = hmac.compare_digest(_norm(passcode).encode(), self._code.encode())
        if ok:
            self._fails.pop(device, None)
        else:
            now = self._clock()
            self._fails[device].append(now)
            self._all.append(now)
        return ok
