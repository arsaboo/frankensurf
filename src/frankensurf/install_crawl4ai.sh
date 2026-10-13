#!/usr/bin/env bash
set -euo pipefail
umask 077

# Ships inside the package: everything it needs sits next to it.
package_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# The Python the environment is built from (frankensurf install passes its own).
base_python="${FRANKENSURF_CRAWL4AI_BASE_PYTHON:-python3}"
provider_dir="${FRANKENSURF_CRAWL4AI_DIR:-$HOME/.local/share/frankensurf/crawl4ai-venv}"
case "$provider_dir" in
  /*) ;;
  *) printf 'FRANKENSURF_CRAWL4AI_DIR must be absolute.\n' >&2; exit 2 ;;
esac

"$base_python" "$package_dir/crawl4ai_preflight.py" "$provider_dir"

lock_file="$package_dir/crawl4ai_requirements.lock.txt"
worker="$package_dir/crawl4ai_worker.py"
browser_dir="$provider_dir/browsers"
health_file="$provider_dir/frankensurf-health.json"
health_home="$provider_dir/health-home"
install_cache="$provider_dir/install-cache"

"$base_python" -m venv "$provider_dir"
provider_python="$provider_dir/bin/python"
mkdir -p "$browser_dir" "$health_home" "$install_cache"
chmod 700 "$provider_dir" "$browser_dir" "$health_home" "$install_cache"

PIP_CACHE_DIR="$install_cache/pip" \
  "$provider_python" -m pip install --disable-pip-version-check \
  --requirement "$lock_file"
"$provider_python" -m pip check

"$provider_python" - "$lock_file" <<'PY'
from importlib import metadata
from pathlib import Path
import re
import sys

from packaging.utils import canonicalize_name

expected = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.fullmatch(r"([^=<>!~ ]+)==([^ ]+)", line)
    if not match:
        raise SystemExit("Lock file must contain exact pins only")
    expected[canonicalize_name(match.group(1))] = match.group(2)
actual = {
    canonicalize_name(item.metadata["Name"]): item.version
    for item in metadata.distributions()
    if item.metadata.get("Name")
}
for ignored in ("pip", "setuptools"):
    actual.pop(ignored, None)
if actual != expected:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    wrong = sorted(name for name in set(actual) & set(expected)
                   if actual[name] != expected[name])
    raise SystemExit(
        f"Installed Crawl4AI environment does not match lock: "
        f"missing={missing}, extra={extra}, wrong={wrong}")
PY

export PLAYWRIGHT_BROWSERS_PATH="$browser_dir"
export PATCHRIGHT_BROWSERS_PATH="$browser_dir"
export PIP_CACHE_DIR="$install_cache/pip"
export XDG_CACHE_HOME="$install_cache/xdg"
"$provider_python" -m playwright install chromium
"$provider_python" -m patchright install chromium

health_tmp="$(mktemp "$provider_dir/.frankensurf-health.XXXXXX")"
cleanup() { rm -f "$health_tmp"; }
trap cleanup EXIT
HOME="$health_home" \
XDG_CACHE_HOME="$health_home/.cache" \
XDG_CONFIG_HOME="$health_home/.config" \
XDG_DATA_HOME="$health_home/.local/share" \
CRAWL4AI_BASE_DIRECTORY="$health_home/crawl4ai" \
PLAYWRIGHT_BROWSERS_PATH="$browser_dir" \
PATCHRIGHT_BROWSERS_PATH="$browser_dir" \
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "$provider_python" "$worker" --health > "$health_tmp"

lock_sha256="$("$provider_python" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$lock_file")"
"$provider_python" - "$health_tmp" "$health_file" "$provider_python" \
  "$browser_dir" "$lock_sha256" \
  "$package_dir/crawl4ai_config.py" "$provider_dir" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
python = Path(sys.argv[3]).absolute()
python.resolve(strict=True)
browsers = Path(sys.argv[4]).resolve(strict=True)
lock_hash = sys.argv[5]
config_path = Path(sys.argv[6]).resolve(strict=True)
provider_root = Path(sys.argv[7]).resolve(strict=True)
payload = json.loads(source.read_text())
if (type(payload) is not dict
        or set(payload) != {
            "schema", "status", "sdk_version", "browser_executable"}
        or payload.get("schema") != "frankensurf.crawl4ai-health/v1"
        or payload.get("status") != "ok"
        or payload.get("sdk_version") != "0.9.4"):
    raise SystemExit("Crawl4AI worker health failed")
browser = Path(payload["browser_executable"]).resolve(strict=True)
browser.relative_to(browsers)
spec = importlib.util.spec_from_file_location(
    "frankensurf_crawl4ai_install_config", config_path)
if spec is None or spec.loader is None:
    raise SystemExit("Cannot load Crawl4AI identity validator")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
identity = config._installation_identity(
    provider_root, python, browsers, browser)
if identity is None:
    raise SystemExit("Crawl4AI installation identity validation failed")
stamp = {
    "schema": payload["schema"],
    "status": "ok",
    "sdk_version": payload["sdk_version"],
    "python": str(python),
    "browsers_path": str(browsers),
    "browser_executable": str(browser),
    "requirements_sha256": lock_hash,
    **identity,
}
source.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n")
os.chmod(source, 0o600)
os.replace(source, destination)
os.chmod(destination, 0o600)
PY
trap - EXIT

printf 'Crawl4AI %s worker ready at %s with isolated browsers at %s.\n' \
  '0.9.4' "$provider_dir" "$browser_dir"
