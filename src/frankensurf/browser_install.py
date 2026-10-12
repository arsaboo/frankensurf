"""Checks around the browser builds FrankenSurf downloads: pinned checksums for the
direct Camoufox download, and macOS quarantine on the app bundles it installed.

Standard library only: the direct download runs this file inside the provider
environment, which does not have FrankenSurf installed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

# SHA-256 of the pinned Camoufox build's release assets (github.com/daijro/camoufox,
# tag font-bundle-v1), by "<os>.<arch>" as Camoufox names them. The direct download
# refuses any platform or version missing here. When CAMOUFOX_BROWSER changes, download
# the new assets once, hash them, and add the entry; `camoufox fetch` is unaffected.
CAMOUFOX_SHA256 = {
    "152.0.4-beta.31": {
        "lin.arm64": "98dbffa9b687d42693dd0abebc47737e651b9e4b6991ba8571c3c29062645c04",
        "lin.x86_64": "3a7958c84c0c1962574bb177a10a247b614823557308ccb7cf2e2bc9ce406c55",
        "mac.arm64": "7b8d12d61de9a9fbd3ce9c034399ba5307e44f4d8951873cc53ad39d20957737",
        "mac.x86_64": "4a6f02ffc1992a41306b00851fc0ef339ff555c92a547fe865690f89b58d8840",
        "win.i686": "28fcda907dc86e6524cd1fac6eab2c0735e0c0eca2f29d6795cad2efccab68a1",
        "win.x86_64": "ed63ea51d2a07f99bacdae0f43b9ff5518bfc9ac2f15d84e2251fe06c7141e81",
    },
}

# Extra seconds for the very first launch of a browser after install: macOS
# Gatekeeper verifies an unsigned or quarantined app bundle once, and can take a while.
FIRST_LAUNCH_EXTRA_SECONDS = 90


def pinned_sha256(version, platform_key):
    """The expected SHA-256 for one pinned build asset; ValueError when none is pinned."""
    expected = CAMOUFOX_SHA256.get(version, {}).get(platform_key)
    if not expected:
        raise ValueError(
            f"error: no SHA-256 is pinned for Camoufox {version} ({platform_key}); refusing the direct "
            "download. Maintainers: add the hash to CAMOUFOX_SHA256 in frankensurf/browser_install.py")
    return expected


def verify_sha256(archive, expected, label="download"):
    """Check an open binary file against a SHA-256; ValueError on a mismatch."""
    digest = hashlib.sha256()
    archive.seek(0)
    while chunk := archive.read(1 << 20):
        digest.update(chunk)
    archive.seek(0)
    if digest.hexdigest() != expected.lower():
        raise ValueError(f"error: SHA-256 mismatch for {label}: expected {expected}, got {digest.hexdigest()}; "
                         "the download was discarded")


def owned_bundles(home=None):
    """The macOS app bundles FrankenSurf installs, only inside its own cache paths."""
    caches = Path(home or Path.home()) / "Library" / "Caches"
    found = list((caches / "camoufox" / "browsers" / "official").glob("*/Camoufox.app"))
    playwright = caches / "ms-playwright"
    found += list(playwright.glob("chromium-*/*/*.app"))
    found += list(playwright.glob("chromium_headless_shell-*/*"))
    return [path for path in found if path.is_dir()]


def clear_quarantine(bundles=None, log=print):
    """Remove com.apple.quarantine from FrankenSurf's own browser bundles (macOS only).

    Browsers downloaded by tools that set the attribute make Gatekeeper verify
    the app on first launch: slow, or blocked for an unsigned bundle. Returns
    the bundles it cleared."""
    if sys.platform != "darwin":
        return []
    cleared = []
    for bundle in owned_bundles() if bundles is None else bundles:
        try:
            marked = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(bundle)],
                                    capture_output=True, timeout=60).returncode == 0
            if not marked:
                continue
            done = subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(bundle)],
                                  capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            log(f"  could not check the quarantine flag on {bundle}")
            continue
        if done.returncode == 0:
            log(f"  removed the macOS quarantine flag from {bundle}")
            cleared.append(bundle)
        else:
            log(f"  could not remove the macOS quarantine flag from {bundle}")
    return cleared


def _marker(root, provider):
    return Path(root) / ".launched" / provider


def first_launch(root, provider):
    """True on macOS until this browser has read a page since it was installed."""
    return sys.platform == "darwin" and not _marker(root, provider).exists()


def mark_launched(root, provider):
    try:
        marker = _marker(root, provider)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def forget_launches(root):
    """After an install: the next launch of each browser is a first launch again."""
    for marker in (Path(root) / ".launched").glob("*"):
        try:
            marker.unlink()
        except OSError:
            pass
