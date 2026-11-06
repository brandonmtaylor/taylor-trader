#!/usr/bin/env bash
# Build the dashboard, then the Python package with the dashboard inside it.
# Output: dist/taylortrader-<version>-py3-none-any.whl and the .tar.gz. Publish with `uv publish`.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd dashboard && npm ci && npm run build)
test -f src/taylortrader/web/static/index.html || { echo "dashboard build missing" >&2; exit 1; }
rm -rf dist
uv build
# The wheel must carry the dashboard, or installs open a blank page.
python3 - <<'PY'
import glob, sys, zipfile
whl = glob.glob("dist/*.whl")[0]
names = zipfile.ZipFile(whl).namelist()
if "taylortrader/web/static/index.html" not in names:
    sys.exit(f"{whl} is missing the dashboard")
bad = [n for n in names if n.endswith((".env", "config.toml")) or "/var/" in n]
if bad:
    sys.exit(f"{whl} contains files that must never ship: {bad}")
print(f"ok: {whl} ({len(names)} files, dashboard included)")
PY
