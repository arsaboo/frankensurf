"""Platform-specific install logic: pinned checksums, macOS quarantine, first launch."""
import hashlib
import io
import subprocess
import sys
import types
import zipfile

import pytest

from frankensurf import browser_install, experimental

VERSION = experimental.CAMOUFOX_BROWSER.split("/")[1]


def test_every_platform_of_the_pinned_build_has_a_sha256():
    pinned = browser_install.CAMOUFOX_SHA256[VERSION]
    assert set(pinned) == {"lin.arm64", "lin.x86_64", "mac.arm64", "mac.x86_64", "win.i686", "win.x86_64"}
    for digest in pinned.values():
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert browser_install.pinned_sha256(VERSION, "mac.arm64") == pinned["mac.arm64"]


def test_a_missing_hash_fails_closed_and_tells_maintainers_what_to_do():
    with pytest.raises(ValueError, match="CAMOUFOX_SHA256"):
        browser_install.pinned_sha256("999.0.0-beta.1", "mac.arm64")
    with pytest.raises(ValueError, match="no SHA-256 is pinned"):
        browser_install.pinned_sha256(VERSION, "lin.riscv")


def test_verify_sha256_passes_a_matching_archive_and_rejects_a_changed_one():
    body = b"archive bytes" * 1000
    good = hashlib.sha256(body).hexdigest()
    archive = io.BytesIO(body)
    browser_install.verify_sha256(archive, good.upper(), "asset")
    assert archive.tell() == 0, "left rewound for the unzip that follows"
    with pytest.raises(ValueError, match="mismatch"):
        browser_install.verify_sha256(io.BytesIO(body + b"!"), good, "asset")


def _run_direct_download(monkeypatch, tmp_path, body, pinned):
    """Run the fallback download script against stand-ins for camoufox's helpers."""
    cache = tmp_path / "camoufox"
    fake = {
        "orjson": types.SimpleNamespace(dumps=lambda value: b"{}"),
        "camoufox": types.ModuleType("camoufox"),
        "camoufox.multiversion": types.SimpleNamespace(BROWSERS_DIR=cache),
        "camoufox.pkgman": types.SimpleNamespace(
            OS_NAME="mac",
            RepoConfig=types.SimpleNamespace(get_default=lambda: types.SimpleNamespace(
                get_os_name=lambda: "mac", get_arch=lambda: "arm64")),
            webdl=lambda url, buffer: buffer.write(body),
            unzip=lambda archive, path: zipfile.ZipFile(archive).extractall(path)),
        "browser_install": browser_install,
    }
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(browser_install, "CAMOUFOX_SHA256", {VERSION: {"mac.arm64": pinned}} if pinned else {})
    monkeypatch.setattr(sys, "argv", ["-c", VERSION, "ignored"])
    monkeypatch.setattr("os.system", lambda command: 0)
    exec(compile(experimental._CAMOUFOX_DIRECT, "direct", "exec"), {"__name__": "__main__"})
    return cache / "official" / (VERSION + "-direct")


def _zip_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Camoufox.app/Contents/MacOS/camoufox", "x")
    return out.getvalue()


def test_the_direct_download_unpacks_an_archive_that_matches_its_pin(monkeypatch, tmp_path):
    body = _zip_bytes()
    build = _run_direct_download(monkeypatch, tmp_path, body, hashlib.sha256(body).hexdigest())
    assert (build / "Camoufox.app/Contents/MacOS/camoufox").is_file() and (build / "version.json").is_file()


def test_the_direct_download_refuses_and_deletes_a_mismatching_archive(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as refused:
        _run_direct_download(monkeypatch, tmp_path, _zip_bytes(), "0" * 64)
    assert "SHA-256 mismatch" in str(refused.value)
    assert not (tmp_path / "camoufox/official" / (VERSION + "-direct")).exists()


def test_the_direct_download_without_a_pinned_hash_refuses_before_downloading(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as refused:
        _run_direct_download(monkeypatch, tmp_path, _zip_bytes(), None)
    assert "no SHA-256 is pinned" in str(refused.value)


def _xattr(monkeypatch, marked):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        code = (0 if str(command[-1]) in marked else 1) if command[1] == "-p" else 0
        return subprocess.CompletedProcess(command, code, stdout=b"", stderr=b"")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_quarantine_is_removed_from_a_marked_bundle_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle, clean = tmp_path / "Camoufox.app", tmp_path / "Other.app"
    calls = _xattr(monkeypatch, {str(bundle)})
    logged = []
    assert browser_install.clear_quarantine([bundle, clean], log=logged.append) == [bundle]
    assert ["xattr", "-dr", "com.apple.quarantine", str(bundle)] in calls
    assert not any(command[1] == "-dr" and str(command[-1]) == str(clean) for command in calls)
    assert len(logged) == 1 and "quarantine" in logged[0] and str(bundle) in logged[0]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_quarantine_is_left_alone_off_macos(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(sys, "platform", platform)
    calls = _xattr(monkeypatch, {str(tmp_path)})
    assert browser_install.clear_quarantine([tmp_path]) == [] and calls == []
    assert not browser_install.first_launch(tmp_path, "camoufox")


def test_a_failed_or_missing_xattr_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    logged = []

    def broken(command, **kwargs):
        raise FileNotFoundError("xattr")
    monkeypatch.setattr(subprocess, "run", broken)
    assert browser_install.clear_quarantine([tmp_path], log=logged.append) == []
    assert "could not check" in logged[0]
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(
        command, 1 if command[1] == "-dr" else 0, stdout=b"", stderr=b""))
    logged.clear()
    assert browser_install.clear_quarantine([tmp_path], log=logged.append) == []
    assert "could not remove" in logged[0]


def test_only_bundles_in_frankensurfs_own_caches_are_cleared(monkeypatch, tmp_path):
    caches = tmp_path / "Library/Caches"
    ours = [caches / "camoufox/browsers/official/152.0.4-beta.31-abc/Camoufox.app",
            caches / "ms-playwright/chromium-1200/chrome-mac-arm64/Google Chrome for Testing.app"]
    others = [tmp_path / "Applications/Google Chrome.app", caches / "ms-playwright/firefox-1/firefox.app",
              caches / "camoufox/browsers/official/other/Notes.app"]
    for path in ours + others:
        path.mkdir(parents=True)
    assert sorted(browser_install.owned_bundles(tmp_path)) == sorted(ours)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    calls = _xattr(monkeypatch, {str(path) for path in ours + others})
    browser_install.clear_quarantine(log=lambda message: None)
    cleared = {command[-1] for command in calls if command[1] == "-dr"}
    assert cleared == {str(path) for path in ours}


def test_the_first_launch_after_install_is_marked_and_reset_by_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert browser_install.first_launch(tmp_path, "camoufox")
    browser_install.mark_launched(tmp_path, "camoufox")
    assert not browser_install.first_launch(tmp_path, "camoufox")
    assert browser_install.first_launch(tmp_path, "patchright")
    browser_install.forget_launches(tmp_path)
    assert browser_install.first_launch(tmp_path, "camoufox")


def test_the_worker_widens_playwrights_launch_timeout_only_when_asked():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("provider_worker", Path(experimental.__file__).with_name("provider_worker.py"))
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    assert worker._launch_timeout({}) == {}
    assert worker._launch_timeout({"launch_timeout_seconds": 120}) == {"timeout": 120000.0}


def _provider_env(monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/python").write_text("")
    (tmp_path / "lib/python3.12/site-packages/camoufox").mkdir(parents=True)
    monkeypatch.setenv("FRANKENSURF_PROVIDER_PYTHON", str(tmp_path / "bin/python"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")


def test_nodriver_is_reported_as_optional_and_never_installed_by_setup(monkeypatch, tmp_path):
    _provider_env(monkeypatch, tmp_path)
    commands = []
    monkeypatch.setattr(experimental, "_run_step", lambda command, log, **kw: commands.append(command))
    result = experimental.install_free_providers(log=lambda message: None)
    assert result["nodriver"] == "not installed (optional)"
    assert not any(str(part).startswith("nodriver") for command in commands for part in command)
    (tmp_path / "lib/python3.12/site-packages/nodriver").mkdir()
    assert experimental.install_free_providers(log=lambda message: None)["nodriver"] is True


async def test_reading_with_nodriver_pinned_says_it_is_not_installed(monkeypatch, tmp_path):
    from frankensurf.runtime import WebFailure
    _provider_env(monkeypatch, tmp_path)
    policy = types.SimpleNamespace(identity=None, allow_local_browser=True)
    with pytest.raises(WebFailure) as failure:
        await experimental.read_public(None, "https://example.com", policy, "nodriver")
    assert failure.value.code == "PROVIDER_UNAVAILABLE" and "not installed (optional)" in str(failure.value)
