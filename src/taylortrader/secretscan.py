"""Scan files for credentials and personal details before they can be committed.

Usage:
  python -m taylortrader.secretscan                  # all tracked + untracked, non-ignored files
  python -m taylortrader.secretscan --staged         # what's about to be committed (pre-commit hook)
  python -m taylortrader.secretscan --message FILE   # a commit message (commit-msg hook)
Exit code 1 if anything looks like a secret or a personal detail. Findings show the file and line, never the value.

Personal details: besides the built-in checks (home-folder paths, real-looking email addresses, files that hold
account data), each person can list their own words to keep out of the repo (name, email, account number, private
project names...) in ~/.config/taylortrader/private-words.txt, one per line, matched anywhere ignoring case. That
file lives outside the repo, so the list itself is never published. TAYLORTRADER_PRIVATE_WORDS overrides its path.
"""

from __future__ import annotations

import os
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
    "API key": re.compile(r"\bsk-(?:ant-|proj-)[A-Za-z0-9_\-]{20,}"),
    "ntfy topic URL": re.compile(r"ntfy\.sh/[A-Za-z0-9_\-]{8,}"),
    "healthcheck URL": re.compile(r"hc-ping\.com/[0-9a-f\-]{20,}"),
}
PERSONAL = {
    # /Users/<name>/ or /home/<name>/: a real home folder (placeholders like /Users/you/ are fine)
    "home folder path": re.compile(r"(?<![\w.])/(?:Users|home)/(?!(?:you|me|user|runner|USER|name)/)[A-Za-z0-9._-]+/"),
}
EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
# Placeholder emails that are fine in docs and examples.
EMAIL_OK_LOCAL = {"you", "me", "name", "user", "someone", "your.name", "yourname", "noreply", "no-reply"}
EMAIL_OK_DOMAIN = re.compile(r"(?i)(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.test|users\.noreply\.github\.com)$")
# Files that hold this machine's settings, account data or logs: never committed, whatever they contain.
PERSONAL_FILE = re.compile(r"(^|/)(config\.toml|[^/]*\.bak|[^/]*\.(db|sqlite|sqlite3|log))$|(^|/)var/")
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm"}
MEDIA_DIRS = ("docs/images/",)  # screenshots belong here, after checking they show a made-up account
PRIVATE_WORDS_FILE = Path("~/.config/taylortrader/private-words.txt")
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".parquet", ".pdf"}
SKIP_FILES = {"LICENSE"}
ALLOW_MARKER = "secretscan: allow"
# A GitHub Action pinned to a commit (uses: owner/repo@<40 hex>): the hash looks like an Alpaca secret.
PINNED_ACTION = re.compile(r"^\s*(-\s*)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}(\s+#.*)?$")
# The allow marker can silence generic patterns, never these.
NEVER_ALLOWED = {"Alpaca key ID", "private key", "API key", "known credential value", "private word"}
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


def private_words() -> set[str]:
    """This person's own list of words to keep out of the repo (lower-cased), from a file outside it."""
    path = Path(os.environ.get("TAYLORTRADER_PRIVATE_WORDS") or PRIVATE_WORDS_FILE).expanduser()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    return {w.strip().lower() for w in lines if len(w.strip()) >= 3 and not w.strip().startswith("#")}


def _email_is_personal(m: re.Match) -> bool:
    local, domain = m.group(1).lower(), m.group(2).lower()
    return local not in EMAIL_OK_LOCAL and not EMAIL_OK_DOMAIN.search(domain)


def path_findings(path: Path) -> list[str]:
    """Problems with a file's name or place alone, before reading it."""
    p = path.as_posix()
    if PERSONAL_FILE.search(p):
        return ["personal file (settings, account data or logs) should never be committed"]
    if path.suffix.lower() in MEDIA_SUFFIXES and not p.startswith(MEDIA_DIRS):
        return [f"image or video outside {', '.join(MEDIA_DIRS)}: check it shows no real account, then move it there"]
    return []


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def files_to_scan(staged: bool) -> list[Path]:
    if staged:
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        names = _git("ls-files", "--cached", "--others", "--exclude-standard")
    return [Path(n) for n in names]


def scan_text(text: str, known: set[str] | frozenset[str] = frozenset(),
              words: set[str] | frozenset[str] = frozenset()) -> list[tuple[int, str]]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        allowed = ALLOW_MARKER in line
        if any(value in line for value in known):
            hits.append((lineno, "known credential value"))
        low = line.lower()
        if any(w in low for w in words):
            hits.append((lineno, "private word"))
        if not allowed:
            hits.extend((lineno, label) for label, pattern in PERSONAL.items() if pattern.search(line))
            if any(_email_is_personal(m) for m in EMAIL.finditer(line)):
                hits.append((lineno, "email address"))
        checked = line.rsplit("@", 1)[0] if PINNED_ACTION.match(line) else line
        for label, pattern in PATTERNS.items():
            if allowed and label not in NEVER_ALLOWED:
                continue
            if pattern.search(checked):
                hits.append((lineno, label))
    return hits


def staged_text(path: Path) -> str | None:
    """What's actually being committed, which can differ from the file on disk."""
    r = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=False)
    try:
        return r.stdout.decode() if r.returncode == 0 else None
    except UnicodeDecodeError:
        return None


def scan(paths: list[Path], known: set[str] | None = None, staged: bool = False,
         words: set[str] | None = None) -> list[tuple[Path, int, str]]:
    known = known_secret_values() if known is None else known
    words = private_words() if words is None else words
    findings = []
    for path in paths:
        if bad := path_findings(path):
            findings.extend((path, 0, label) for label in bad)
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_FILES or (not staged and not path.is_file()):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            findings.append((path, 0, ".env file should never be committed"))
            continue
        try:
            text = staged_text(path) if staged else path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if text is None:
            continue
        findings.extend((path, lineno, label) for lineno, label in scan_text(text, known, words))
    return findings


def scan_message(text: str, known: set[str] | None = None, words: set[str] | None = None) -> list[tuple[int, str]]:
    """A commit message: git's own comment lines are dropped, everything else is checked like a file."""
    known = known_secret_values() if known is None else known
    words = private_words() if words is None else words
    kept = "\n".join("" if line.startswith("#") else line for line in text.splitlines())
    return scan_text(kept, known, words)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--message" in argv:
        name = argv[argv.index("--message") + 1]
        hits = scan_message(Path(name).read_text())
        for lineno, label in hits:
            print(f"commit message line {lineno}: {label}", file=sys.stderr)
        if hits:
            print("\nThe commit message has a possible secret or personal detail. Reword it and commit again.",
                  file=sys.stderr)
            return 1
        return 0
    staged = "--staged" in argv
    findings = scan(files_to_scan(staged=staged), staged=staged)
    for path, lineno, label in findings:
        print(f"blocked: {path}{f':{lineno}' if lineno else ''}: {label}", file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} possible secret(s) or personal detail(s). Move secrets to .env or the Keychain and "
            f"take personal details out. If a line is a false positive, add a '# {ALLOW_MARKER}' comment to it "
            "(that never silences a key, a known credential or a private word).",
            file=sys.stderr,
        )
        return 1
    print("secretscan: no secrets or personal details found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
