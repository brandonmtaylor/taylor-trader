"""Scan files for credentials before they can be committed.

Usage:
  python -m taylortrader.secretscan            # all tracked + untracked, non-ignored files
  python -m taylortrader.secretscan --staged   # what's about to be committed (pre-commit hook)
Exit code 1 if anything looks like a secret. Findings show the file and line, never the value.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Alpaca key ID": re.compile(r"\b(?:AK|PK|CK)[A-Z0-9]{16,24}\b"),
    "Alpaca secret (40 chars)": re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9]{40}(?![A-Za-z0-9/+])"),
    "secret assignment": re.compile(
        r"(?i)\b[A-Z_]*(SECRET|API_KEY|KEY_ID|PASSWORD|TOKEN)[A-Z_]*\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,}"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".parquet", ".lock", ".svg", ".pdf"}
SKIP_FILES = {"LICENSE"}
ALLOW_MARKER = "secretscan: allow"
# The allow marker can silence generic patterns, never these.
NEVER_ALLOWED = {"Alpaca key ID", "private key", "known credential value"}
KNOWN_SECRET_NAMES = (
    "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY",
    "NTFY_TOPIC", "EMAIL_PASSWORD", "HEALTHCHECK_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
)


def known_secret_values() -> set[str]:
    """Exact values of the credentials configured on this machine (env, .env, Keychain)."""
    from taylortrader.secrets import get_secret

    values = set()
    for name in KNOWN_SECRET_NAMES:
        secret = get_secret(name)
        if secret is not None and len(secret.get_secret_value()) >= 12:
            values.add(secret.get_secret_value())
    return values


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def files_to_scan(staged: bool) -> list[Path]:
    if staged:
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        names = _git("ls-files", "--cached", "--others", "--exclude-standard")
    return [Path(n) for n in names]


def scan_text(text: str, known: set[str] | frozenset[str] = frozenset()) -> list[tuple[int, str]]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        allowed = ALLOW_MARKER in line
        if any(value in line for value in known):
            hits.append((lineno, "known credential value"))
        for label, pattern in PATTERNS.items():
            if allowed and label not in NEVER_ALLOWED:
                continue
            if pattern.search(line):
                hits.append((lineno, label))
    return hits


def scan(paths: list[Path], known: set[str] | None = None) -> list[tuple[Path, int, str]]:
    known = known_secret_values() if known is None else known
    findings = []
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_FILES or not path.is_file():
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            findings.append((path, 0, ".env file should never be committed"))
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend((path, lineno, label) for lineno, label in scan_text(text, known))
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    findings = scan(files_to_scan(staged="--staged" in argv))
    for path, lineno, label in findings:
        print(f"possible secret: {path}:{lineno}: {label}", file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} possible secret(s). Move them to .env or the Keychain. "
            f"If a line is a false positive, add a '# {ALLOW_MARKER}' comment to it.",
            file=sys.stderr,
        )
        return 1
    print("secretscan: no secrets found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
