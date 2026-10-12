"""frankensurf install: every free tool in one command, like `playwright install`. Nothing
here downloads or starts anything real: subprocesses, the network and the native search
engine's process are all faked (conftest points the native engine at a temporary folder)."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import httpx
import pytest

from frankensurf import cli, installer, local_search
from frankensurf.cli import parse_args


def ran_commands(monkeypatch, code=0, out=""):
    ran = []

    def run(command, **kwargs):
        ran.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, code, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    return ran


# --- choosing what to install ----------------------------------------------------------------

def test_nothing_named_or_all_means_every_free_tool():
    assert installer.resolve([]) == ["chromium", "stealth", "search", "crawl4ai"]
    assert installer.resolve(["all"]) == installer.resolve(None)
    assert installer.resolve(["search", "Chromium", "search"]) == ["search", "chromium"]
    assert installer.resolve(["nodriver"]) == ["nodriver"]       # optional: only when named
    with pytest.raises(ValueError, match="choose from all, chromium"):
        installer.resolve(["firefox"])


def test_install_and_uninstall_arguments():
    assert parse_args(["install"]).urls == []
    assert parse_args(["install", "search", "--with-deps"]).with_deps is True
    assert parse_args(["install", "--list"]).list is True
    for bad in (["install", "firefox"], ["uninstall"], ["install", "--list", "search"],
                ["read", "https://example.com", "--with-deps"], ["setup", "--list"]):
        with pytest.raises(SystemExit):
            parse_args(bad)
    assert parse_args(["uninstall", "all"]).urls == ["all"]


# --- installing ------------------------------------------------------------------------------

def test_one_tool_failing_never_stops_the_others(monkeypatch):
    done = []

    def works(name):
        return lambda log: done.append(name) or {"ok": True}
    monkeypatch.setattr(installer, "INSTALLERS", {
        "chromium": works("chromium"), "stealth": lambda log: (_ for _ in ()).throw(OSError("disk full")),
        "search": works("search"), "crawl4ai": lambda log: {"ok": False, "error": "no bash"}})
    result = installer.install(log=lambda message: None)
    assert done == ["chromium", "search"]
    assert result["ok"] is False
    assert result["installed"]["stealth"] == {"ok": False, "error": "disk full"}
    assert result["retry_with"] == "frankensurf install stealth crawl4ai"
    assert json.loads(json.dumps(result)) == result


def test_chromium_is_playwrights_own_install(monkeypatch):
    ran = ran_commands(monkeypatch)
    assert installer._install_chromium(lambda message: None) == {"ok": True}
    assert ran[0][1:] == ["-m", "playwright", "install", "chromium"]


def test_with_deps_installs_the_browsers_system_libraries_on_linux(monkeypatch):
    ran = ran_commands(monkeypatch)
    monkeypatch.setattr(installer, "INSTALLERS", {"chromium": lambda log: {"ok": True}})
    monkeypatch.setattr(installer.sys, "platform", "linux")
    result = installer.install(["chromium"], with_deps=True, log=lambda message: None)
    assert ran[0][1:] == ["-m", "playwright", "install-deps", "chromium", "firefox"]
    assert result["system_libraries"] is True and result["ok"] is True
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    assert installer.install(["chromium"], with_deps=True, log=lambda message: None)[
        "system_libraries"] == "not needed on this system"


def test_crawl4ai_runs_the_installer_that_ships_in_the_package(monkeypatch):
    from frankensurf import crawl4ai_config
    ran = []
    monkeypatch.setattr(installer, "_run", lambda command, **kwargs: ran.append((command, kwargs["env"])) or 0)
    monkeypatch.setattr(crawl4ai_config, "installed", lambda: True)
    monkeypatch.setattr(installer, "_crawl4ai_base_python", lambda: "/usr/bin/python3.12")
    assert installer._install_crawl4ai(lambda message: None)["ok"] is True
    (command, env), = ran
    assert command[0] == "bash" and Path(command[1]).name == "install_crawl4ai.sh" and Path(command[1]).is_file()
    assert env["FRANKENSURF_CRAWL4AI_BASE_PYTHON"] == "/usr/bin/python3.12"
    # Installed but refused by the environment checks: said plainly, not as a silent failure.
    monkeypatch.setattr(crawl4ai_config, "installed", lambda: False)
    assert "failed FrankenSurf's checks" in installer._install_crawl4ai(lambda message: None)["error"]


def test_crawl4ai_is_built_from_a_python_no_other_user_can_change(monkeypatch, tmp_path):
    shared = tmp_path / "python-shared"
    shared.write_text("")
    shared.chmod(0o775)
    own = tmp_path / "python-own"
    own.write_text("")
    own.chmod(0o755)
    assert installer._protected(own) and not installer._protected(shared)
    monkeypatch.setattr(installer.sys, "_base_executable", str(shared), raising=False)
    monkeypatch.setattr(installer.sys, "executable", str(shared))
    monkeypatch.setattr(installer.shutil, "which", lambda name: str(own))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    assert installer._crawl4ai_base_python() == str(own.resolve())
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer, "_protected", lambda python: False)
    assert installer._crawl4ai_base_python() is None


def test_the_crawl4ai_installer_and_lock_are_package_data():
    from frankensurf import crawl4ai_config
    package = Path(installer.__file__).parent
    lock = crawl4ai_config._lock_path()
    assert lock.parent == package.resolve() and lock.is_file()
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == crawl4ai_config.LOCK_SHA256
    script = (package / "install_crawl4ai.sh").read_text()
    assert "repo_dir" not in script and "sha256sum" not in script     # runs from a pip install, on macOS too
    pyproject = (package.parents[1] / "pyproject.toml").read_text()
    assert '"crawl4ai_requirements.lock.txt"' in pyproject and '"install_crawl4ai.sh"' in pyproject


async def test_install_prints_json_only_and_checks_the_browsers_it_installed(monkeypatch, capsys):
    monkeypatch.setattr(installer, "INSTALLERS", {"chromium": lambda log: log("progress") or {"ok": True}})
    checked = []

    async def works(result, **kwargs):
        checked.append(result)
        return {"local": True}
    monkeypatch.setattr(cli, "_browsers_work", works)
    await cli.run(parse_args(["install", "chromium"]))
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["installed"] == {"chromium": {"ok": True}} and result["works_here"] == {"local": True}
    assert "progress" in captured.err


# --- what is installed -----------------------------------------------------------------------

def test_list_reads_where_playwright_keeps_chromium(monkeypatch, tmp_path):
    (tmp_path / "chromium-1200").mkdir()
    ran_commands(monkeypatch, out=f"browser: chromium version 150\n  Install location:    {tmp_path / 'chromium-1200'}\n")
    assert installer._chromium_status() == {"installed": True, "where": str(tmp_path / "chromium-1200")}
    ran_commands(monkeypatch, out=f"  Install location:    {tmp_path / 'chromium-9999'}\n")
    assert installer._chromium_status()["installed"] is False


def test_list_names_what_is_missing_and_how_to_install_it(monkeypatch):
    monkeypatch.setattr(installer, "_chromium_status", lambda: {"installed": True})
    listed = installer.listing()
    assert set(listed["components"]) == {"chromium", "stealth", "search", "crawl4ai", "nodriver"}
    assert "search" in listed["missing"] and listed["install_with"].startswith("frankensurf install ")
    assert listed["components"]["search"]["installed"] is False


# --- uninstall -------------------------------------------------------------------------------

def test_uninstall_removes_only_frankensurfs_own_folders(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(installer.Path, "home", lambda: home)
    own = home / ".local" / "share" / "frankensurf" / "crawl4ai-venv"
    own.mkdir(parents=True)
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_DIR", str(own))
    assert installer.uninstall(["crawl4ai"])["uninstalled"]["crawl4ai"] == {"removed": str(own)}
    assert not own.exists()
    yours = tmp_path / "my-own-venv"
    yours.mkdir()
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_DIR", str(yours))
    left = installer.uninstall(["crawl4ai"])["uninstalled"]["crawl4ai"]["removed"]
    assert left["left_alone"] == str(yours) and yours.exists()


# --- the native search engine ----------------------------------------------------------------

def searxng_archive(top="searxng-abc"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, body in ((f"{top}/searx/webapp.py", b"# app\n"), (f"{top}/requirements.txt", b"flask==3.1.3\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def serve(monkeypatch, body):
    monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))


class FakeVenv:
    def __init__(self, **kwargs):
        pass

    def create(self, folder):
        python = local_search._native_python(Path(folder).parent)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("")


def test_native_install_checks_the_pin_unpacks_and_marks_itself_installed(monkeypatch):
    import venv
    body = searxng_archive()
    serve(monkeypatch, body)
    monkeypatch.setattr(local_search, "NATIVE_SHA256", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(venv, "EnvBuilder", FakeVenv)
    ran = ran_commands(monkeypatch)
    report = local_search.install_native(log=lambda message: None)
    root = local_search.native_dir()
    assert report == {"status": "installed", "path": str(root), "commit": local_search.NATIVE_COMMIT}
    assert (root / "src" / "searx" / "webapp.py").is_file() and local_search.native_installed()
    assert ran[0][1:] == ["-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                          "-r", str(root / "src" / "requirements.txt")]
    assert local_search.install_native(log=lambda message: None)["already"] is True


def test_native_install_refuses_an_archive_that_does_not_match_the_pin(monkeypatch):
    serve(monkeypatch, searxng_archive())
    ran = ran_commands(monkeypatch)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        local_search.install_native(log=lambda message: None)
    assert not local_search.native_installed() and ran == []
    assert not (local_search.native_dir() / "src").exists()


class FakeProcess:
    pid = 4242

    def poll(self):
        return None


def test_start_native_runs_searxng_on_loopback_in_the_background(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: launched.append((command, kwargs)) or FakeProcess())
    monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "No query"})))
    local_search.native_dir().mkdir(parents=True)
    report = local_search.start_native(folder=tmp_path / "cfg")
    assert report == {"status": "started", "url": local_search.URL, "engine": "native", "pid": 4242}
    (command, kwargs), = launched
    assert command[1:] == ["-m", "searx.webapp"] and kwargs["start_new_session"] is True
    env = kwargs["env"]
    assert env["SEARXNG_BIND_ADDRESS"] == "127.0.0.1" and env["SEARXNG_PORT"] == "8088"
    assert env["SEARXNG_SETTINGS_PATH"] == str(tmp_path / "cfg" / "settings.yml")
    assert (local_search.native_dir() / "searxng.pid").read_text() == "4242"


def test_a_stale_pid_file_is_not_a_running_engine(monkeypatch):
    local_search.native_dir().mkdir(parents=True)
    (local_search.native_dir() / "searxng.pid").write_text("999999999")
    assert local_search.native_pid() is None
    assert local_search.stop_native() is False


def test_autostart_starts_an_installed_engine_once_and_can_be_turned_off(monkeypatch):
    starts = []
    monkeypatch.setattr(local_search, "_autostarted", False)
    monkeypatch.setattr(local_search, "native_installed", lambda: True)
    monkeypatch.setattr(local_search, "start_native", lambda **kwargs: starts.append(1) or {"status": "started"})
    assert local_search.autostart() is True and local_search.autostart() is False
    assert starts == [1]
    monkeypatch.setattr(local_search, "_autostarted", False)
    monkeypatch.setenv("FRANKENSURF_SEARCH_AUTOSTART", "0")
    assert local_search.autostart() is False and starts == [1]
    monkeypatch.delenv("FRANKENSURF_SEARCH_AUTOSTART")
    assert local_search.autostart("http://127.0.0.1:9999") is False   # someone else's engine


def test_autostart_does_nothing_without_a_native_install(monkeypatch):
    monkeypatch.setattr(local_search, "_autostarted", False)
    monkeypatch.setattr(local_search, "start_native", lambda **kwargs: pytest.fail("not installed: nothing to start"))
    assert local_search.autostart() is False


def test_start_prefers_the_native_engine_over_docker(monkeypatch):
    monkeypatch.setattr(local_search, "native_installed", lambda: True)
    monkeypatch.setattr(local_search, "start_native", lambda **kwargs: {"status": "started", "engine": "native"})
    monkeypatch.setattr(local_search, "docker_problem", lambda: pytest.fail("native installed: no Docker"))
    assert local_search.start()["engine"] == "native"
