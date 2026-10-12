"""FRANKENSURF_PROXY: the owner's proxy for servers, from the environment only."""
import json

import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.runtime import proxy_settings, _browser_proxy

PAGE = "<html><title>Hi</title><body>" + "<p>Text of a public page.</p>" * 50 + "</body></html>"


def test_proxy_urls_are_parsed_and_checked():
    settings = proxy_settings("http://user:p%40ss@proxy.example:8080")
    assert settings["server"] == "http://proxy.example:8080"
    assert settings["username"] == "user" and settings["password"] == "p@ss"
    assert _browser_proxy(settings) == {"server": "http://proxy.example:8080", "username": "user", "password": "p@ss"}
    assert proxy_settings("socks5://proxy.example:1080") == {"url": "socks5://proxy.example:1080",
                                                            "server": "socks5://proxy.example:1080"}
    assert proxy_settings(None) is None
    for bad in ("proxy.example:8080", "ftp://proxy.example:21", "http://proxy.example"):
        with pytest.raises(ValueError):
            proxy_settings(bad)


async def test_reads_go_through_the_proxy_and_say_so_without_leaking_it(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_PROXY", "http://user:secret@proxy.example:8080")
    async with Runtime(tmp_path) as web:
        # The shared HTTP client is mounted on the proxy.
        proxies = [getattr(getattr(mount, "_pool", None), "_proxy_url", None) for mount in web._http._mounts.values()]
        assert proxies and all(url is not None and url.host == b"proxy.example" for url in proxies)
    handle = lambda request: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.read("https://shop.example.com/page", WebPolicy(provider="http"))
    assert result["receipt"]["via_proxy"] is True
    assert "secret" not in json.dumps(result, default=str)


async def test_worker_requests_carry_the_proxy_on_stdin_only(tmp_path, monkeypatch):
    from frankensurf import experimental
    seen = {}

    async def fake_packet(url, policy, provider):
        seen["proxy"] = experimental.runtime_proxy.value
        return {"status": "failed", "failure": "PROVIDER_DOWN"}
    monkeypatch.setattr(experimental, "_packet", fake_packet)
    monkeypatch.setenv("FRANKENSURF_PROXY", "http://user:secret@proxy.example:8080")
    async with Runtime(tmp_path) as web:
        with pytest.raises(Exception):
            await experimental.read_public(web, "https://shop.example.com/", WebPolicy(), "camoufox")
    assert seen["proxy"]["server"] == "http://proxy.example:8080"
    assert experimental.runtime_proxy.value is None


def test_camoufox_is_found_in_each_platform_cache(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "worker", Path(__file__).resolve().parents[1] / "src" / "frankensurf" / "provider_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mac = tmp_path / "Library/Caches/camoufox/browsers/official/152.0.4-beta.31-abc/Camoufox.app/Contents/MacOS"
    mac.mkdir(parents=True)
    (mac / "camoufox").write_text("")
    assert worker._camoufox_binaries("152.0.4-beta.31") == [mac / "camoufox"]


def test_setup_keeps_going_when_one_step_fails(monkeypatch, tmp_path):
    import subprocess
    from frankensurf import experimental
    monkeypatch.setenv("FRANKENSURF_PROVIDER_PYTHON", str(tmp_path / "bin" / "python"))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        failed = "camoufox" in command and "fetch" in command
        return subprocess.CompletedProcess(command, 1 if failed else 0, stdout="", stderr="no build for this OS")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    result = experimental.install_free_providers(log=lambda message: None)
    # pip, two tries at the browser, the direct download, then Patchright's browser
    assert len(calls) == 5 and "Downloading the Camoufox browser" in result["errors"]


def _fake_worker_python(monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    (tmp_path / "lib/python3.12/site-packages/camoufox").mkdir(parents=True)
    monkeypatch.setenv("FRANKENSURF_PROVIDER_PYTHON", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")


def test_camoufox_fetch_that_exits_zero_without_a_browser_is_a_failure(monkeypatch, tmp_path):
    """`camoufox fetch` prints its errors and exits 0 (GitHub rate limit, no network)."""
    import subprocess
    from frankensurf import experimental
    _fake_worker_python(monkeypatch, tmp_path)
    fetches = []

    def run(command, **kwargs):
        fetches.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Syncing...\nError: 403 rate limit exceeded\n"
                                           if "fetch" in command else "", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    logged = []
    result = experimental.install_free_providers(log=logged.append)
    assert result["camoufox"] is False and result["scrapling"] is False
    assert len([c for c in fetches if "fetch" in c]) == 2
    assert any("-c" in c for c in fetches), "the direct download is tried after the fetches"
    assert "rate limit" in result["errors"]["Downloading the Camoufox browser"]
    assert "GITHUB_TOKEN" in result["errors"]["Downloading the Camoufox browser"]


def _mac_build(tmp_path, folder="152.0.4-beta.31-abc"):
    mac = tmp_path / "home/Library/Caches/camoufox/browsers/official" / folder / "Camoufox.app/Contents/MacOS"
    mac.mkdir(parents=True)
    (mac / "camoufox").write_text("")


def test_camoufox_fetch_is_retried_until_the_browser_is_there(monkeypatch, tmp_path):
    import subprocess
    from frankensurf import experimental
    _fake_worker_python(monkeypatch, tmp_path)
    fetches = []

    def run(command, **kwargs):
        if "fetch" in command:
            fetches.append(command)
            if len(fetches) == 2:
                _mac_build(tmp_path)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    result = experimental.install_free_providers(log=lambda message: None)
    assert len(fetches) == 2 and result["camoufox"] is True and "errors" not in result


def test_camoufox_falls_back_to_the_pinned_direct_download(monkeypatch, tmp_path):
    """No release list (API rate limit): the pinned build still downloads."""
    import subprocess
    from frankensurf import experimental
    _fake_worker_python(monkeypatch, tmp_path)

    def run(command, **kwargs):
        if "-c" in command:
            assert command[-2] == "152.0.4-beta.31"
            _mac_build(tmp_path, "152.0.4-beta.31-direct")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = experimental.install_free_providers(log=lambda message: None)
    assert result["camoufox"] is True and "errors" not in result


def test_a_direct_download_is_dropped_once_a_regular_install_duplicates_it(monkeypatch, tmp_path):
    from frankensurf import experimental
    _fake_worker_python(monkeypatch, tmp_path)
    _mac_build(tmp_path, "152.0.4-beta.31-direct")
    assert experimental._camoufox_installed() is True
    _mac_build(tmp_path, "152.0.4-beta.31-abc")
    assert experimental._camoufox_ready() is False  # two builds: the worker would refuse to pick
    assert experimental._camoufox_installed() is True
    root = tmp_path / "home/Library/Caches/camoufox/browsers/official"
    assert [path.name for path in root.iterdir()] == ["152.0.4-beta.31-abc"]
