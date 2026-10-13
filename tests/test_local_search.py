"""frankensurf setup helps start a local search engine (SearXNG in Docker), and the
search receipt says when it was not running. Docker and port 8088 are always faked:
a real SearXNG may be running on the machine that runs these tests."""
import json
import subprocess
import sys

import httpx
import pytest

from frankensurf import Runtime, WebPolicy, cli, experimental, local_search
from frankensurf.cli import parse_args

REAL_DOCKER = local_search._docker      # captured at import, before conftest replaces it
URL = local_search.URL


class FakeDocker:
    """Stands in for subprocess.run and for the HTTP answer at port 8088.

    `up` is whether a JSON-enabled SearXNG answers; running or starting a container turns it on."""

    def __init__(self, monkeypatch, *, installed=True, daemon=True, container=None, image=True,
                 up=False, port_page=None):
        self.calls = []
        self.up, self.container, self.image = up, container, image
        self.installed, self.daemon, self.port_page = installed, daemon, port_page
        monkeypatch.setattr(local_search, "_docker", REAL_DOCKER)
        monkeypatch.setattr(subprocess, "run", self.run)
        monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(self.answer))
        monkeypatch.setattr(local_search.time, "sleep", lambda seconds: None)

    def answer(self, request):
        if self.port_page is not None:
            return self.port_page
        if self.up:
            return httpx.Response(400, json={"error": "No query"})
        raise httpx.ConnectError("refused", request=request)

    def run(self, command, **kwargs):
        if command[0] != "docker":
            return subprocess.CompletedProcess(command, 0)     # e.g. playwright install
        if not self.installed:
            raise FileNotFoundError("docker")
        self.calls.append(command[1:])
        verb = command[1]
        done = lambda code=0, out="", err="": subprocess.CompletedProcess(command, code, out, err)
        if verb == "version":
            return done(0 if self.daemon else 1, "27.0.1" if self.daemon else "", "" if self.daemon else "no daemon")
        if verb == "inspect":
            return done(0, self.container + "\n") if self.container else done(1, err="No such object")
        if verb == "image":
            return done(0 if self.image else 1)
        if verb == "pull":
            self.image = True
            return done()
        if verb == "run":
            self.container, self.up = "running", True
            return done(0, "c0ffee\n")
        if verb == "start":
            self.container, self.up = "running", True
            return done()
        if verb == "stop":
            self.container, self.up = "exited", False
            return done()
        raise AssertionError(f"unexpected docker call {command}")

    def verbs(self):
        return [call[0] for call in self.calls]


class FakeNative:
    """Stands in for the native engine: installing it and starting it (no download, no process)."""

    def __init__(self, monkeypatch, *, installed=False, fail=None):
        self.installed, self.fail, self.installs, self.starts = installed, fail, 0, 0
        monkeypatch.setattr(local_search, "install_native", self.install)
        monkeypatch.setattr(local_search, "native_installed", lambda: self.installed)
        monkeypatch.setattr(local_search, "start_native", self.start)

    def install(self, log=print):
        if self.fail:
            raise RuntimeError(self.fail)
        self.installs += 1
        self.installed = True
        return {"status": "installed", "path": "/stand-in/searxng-native", "commit": local_search.NATIVE_COMMIT}

    def start(self, **kwargs):
        self.starts += 1
        return {"status": "started", "url": URL, "engine": "native", "pid": 4242}


def setup(*flags):
    return cli._setup(parse_args(["setup", "--no-stealth", "--no-install-skill", *flags]))


def terminal(monkeypatch, tty):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(experimental, "install_free_providers", lambda log: {})


# --- local_search itself ---------------------------------------------------------------------

def test_already_running_makes_no_docker_call(monkeypatch):
    fake = FakeDocker(monkeypatch, up=True)
    assert local_search.start() == {"status": "running", "url": URL}
    assert local_search.status() == {"status": "running", "url": URL}
    assert fake.calls == []


def test_the_probe_asks_an_empty_query_and_wants_json_enabled(monkeypatch):
    asked = []

    def answer(request):
        asked.append(dict(request.url.params))
        return httpx.Response(400, json={"error": "No query"})
    monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(answer))
    assert local_search.probe() == "running"
    assert asked == [{"q": "", "format": "json"}]
    for page, expected in ((httpx.Response(403, text="Forbidden"), "json_disabled"),
                           (httpx.Response(200, text="<html>my router</html>"), "other"),
                           (httpx.Response(200, json={"results": [], "query": "x"}), "running"),
                           (httpx.Response(404, text="nope"), "other")):
        monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(lambda request, page=page: page))
        assert local_search.probe() == expected


def test_docker_missing_reports_no_docker(monkeypatch):
    FakeDocker(monkeypatch, installed=False)
    report = local_search.start()
    assert report["status"] == "no_docker" and report["docker"] == "not installed"
    assert report["how_to_start"] == "Run: frankensurf install search (no Docker needed)"
    assert "docs.docker.com" in report["or_with_docker"] and "frankensurf search-engine start" in report["or_with_docker"]
    assert "free sources" in report["without_it"]


def test_docker_installed_but_not_running_is_no_docker(monkeypatch):
    fake = FakeDocker(monkeypatch, daemon=False)
    report = local_search.start()
    assert report["status"] == "no_docker" and report["docker"] == "installed but not running"
    assert fake.verbs() == ["version"]


def test_start_runs_the_pinned_image_on_loopback_with_restart_and_json(monkeypatch, tmp_path):
    fake = FakeDocker(monkeypatch, image=False)
    report = local_search.start(folder=tmp_path / "cfg")
    assert report == {"status": "started", "url": URL, "container": "frankensurf-search",
                      "image": local_search.IMAGE}
    assert fake.verbs() == ["version", "inspect", "image", "pull", "run"]
    run = fake.calls[-1]
    assert run[:4] == ["run", "-d", "--name", "frankensurf-search"]
    assert run[run.index("--restart") + 1] == "unless-stopped"
    assert run[run.index("-p") + 1] == "127.0.0.1:8088:8080"
    assert run[-1] == local_search.IMAGE and "@sha256:" in local_search.IMAGE
    assert run[run.index("-v") + 1] == f"{tmp_path / 'cfg'}:/etc/searxng"
    settings = (tmp_path / "cfg" / "settings.yml").read_text()
    assert "- json" in settings and "limiter: false" in settings and "__SECRET_KEY__" not in settings
    # Idempotent: the next call finds it answering and touches nothing.
    before = len(fake.calls)
    assert local_search.start(folder=tmp_path / "cfg")["status"] == "running"
    assert len(fake.calls) == before


def test_settings_are_shipped_and_an_existing_file_is_kept(tmp_path):
    from pathlib import Path
    assert "json" in local_search.settings_template()
    assert "searxng_settings.yml" in Path(__file__).parent.parent.joinpath("pyproject.toml").read_text()
    folder = local_search.prepare_settings(tmp_path)
    first = (folder / "settings.yml").read_text()
    (folder / "settings.yml").write_text(first + "# mine\n")
    local_search.prepare_settings(tmp_path)
    assert (folder / "settings.yml").read_text().endswith("# mine\n")
    other = local_search.prepare_settings(tmp_path / "second")
    assert (other / "settings.yml").read_text() != first          # a fresh random secret each time


def test_a_stopped_container_is_started_not_recreated(monkeypatch, tmp_path):
    fake = FakeDocker(monkeypatch, container="exited")
    assert local_search.start(folder=tmp_path)["status"] == "started"
    assert "run" not in fake.verbs() and "start" in fake.verbs()


def test_something_else_on_the_port_is_never_replaced(monkeypatch):
    for page, wording in ((httpx.Response(200, text="<html>router</html>"), "not SearXNG"),
                          (httpx.Response(403, text="Forbidden"), "JSON output is off")):
        fake = FakeDocker(monkeypatch, port_page=page)
        report = local_search.start()
        assert report["status"] == "not_running" and wording in report["error"]
        assert fake.calls == []


def test_a_failed_run_is_reported_not_raised(monkeypatch, tmp_path):
    fake = FakeDocker(monkeypatch)
    original = fake.run

    def refuse_run(command, **kwargs):
        if command[:2] == ["docker", "run"]:
            fake.calls.append(command[1:])
            return subprocess.CompletedProcess(command, 125, "", "port is already allocated")
        return original(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", refuse_run)
    report = local_search.start(folder=tmp_path)
    assert report["status"] == "not_running" and "port is already allocated" in report["error"]


def test_a_container_that_never_answers_gives_up(monkeypatch, tmp_path):
    fake = FakeDocker(monkeypatch)
    fake.answer = lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request))
    monkeypatch.setattr(local_search, "_TRANSPORT", httpx.MockTransport(fake.answer))
    clock = iter(range(0, 1000, 40))
    monkeypatch.setattr(local_search.time, "monotonic", lambda: next(clock))
    report = local_search.start(folder=tmp_path, wait=90)
    assert report["status"] == "not_running" and "did not answer" in report["error"]


def test_stop_only_stops_the_named_container(monkeypatch):
    fake = FakeDocker(monkeypatch, container="running", up=True)
    report = local_search.stop(name="frankensurf-search")
    assert report["status"] == "not_running" and fake.calls[-1] == ["stop", "frankensurf-search"]
    idle = FakeDocker(monkeypatch)
    assert local_search.stop()["status"] == "not_running" and "stop" not in idle.verbs()


# --- frankensurf setup -----------------------------------------------------------------------

def test_an_unedited_settings_file_from_an_earlier_release_is_upgraded_keeping_its_key(tmp_path):
    key = "ab" * 32
    old = local_search._SETTINGS_V1.replace("__SECRET_KEY__", key)
    (tmp_path / "settings.yml").write_text(old)
    local_search.prepare_settings(tmp_path)
    upgraded = (tmp_path / "settings.yml").read_text()
    assert f'secret_key: "{key}"' in upgraded and "settings-version: 2" in upgraded
    assert "- name: bing" in upgraded and "SearxEngineTooManyRequests: 900" in upgraded
    # An edited file is the user's: left exactly as it is.
    edited = tmp_path / "edited"
    edited.mkdir()
    (edited / "settings.yml").write_text(old + "  # my change\n")
    local_search.prepare_settings(edited)
    assert (edited / "settings.yml").read_text() == old + "  # my change\n"


def test_the_bundled_settings_spread_load_over_more_engines():
    yaml = pytest.importorskip("yaml")
    settings = yaml.safe_load(local_search.settings_template())
    assert {engine["name"] for engine in settings["engines"]} >= {"bing", "google", "yahoo", "qwant", "mojeek"}
    assert all(engine["disabled"] is False for engine in settings["engines"])
    assert settings["search"]["suspended_times"]["SearxEngineTooManyRequests"] >= 900
    assert "json" in settings["search"]["formats"] and settings["server"]["limiter"] is False


def test_setup_interactive_yes_installs_and_starts_the_native_engine(monkeypatch, capsys):
    terminal(monkeypatch, True)
    fake = FakeDocker(monkeypatch)
    native = FakeNative(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    report = setup()["search"]
    assert report["ok"] is True and report["engine"]["status"] == "started"
    assert native.installs == 1 and native.starts == 1 and "run" not in fake.verbs()
    captured = capsys.readouterr()
    assert "Install a local search engine (SearXNG, about 140 MB, no Docker; runs on 127.0.0.1:8088)? [y/N]" in captured.err
    assert captured.out == ""


def test_setup_interactive_default_is_no(monkeypatch):
    terminal(monkeypatch, True)
    fake = FakeDocker(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    report = setup()["search"]
    assert report["status"] == "declined" and report["how_to_start"] == "frankensurf search-engine start"
    assert "run" not in fake.verbs()


def test_setup_search_flag_installs_without_asking(monkeypatch):
    terminal(monkeypatch, False)
    FakeDocker(monkeypatch)
    native = FakeNative(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--search must not prompt"))
    assert setup("--search")["search"]["ok"] is True
    assert native.installs == 1


def test_setup_without_a_terminal_never_asks_or_starts(monkeypatch):
    terminal(monkeypatch, False)
    fake = FakeDocker(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt without a terminal"))
    report = setup()["search"]
    assert report["status"] == "not_running" and "--search" in report["note"]
    assert report["how_to_start"] == "frankensurf search-engine start"
    assert "run" not in fake.verbs()


def test_setup_in_ci_never_asks_or_starts(monkeypatch):
    terminal(monkeypatch, True)
    monkeypatch.setenv("CI", "true")
    fake = FakeDocker(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt in CI"))
    assert setup()["search"]["status"] == "not_running"
    assert "run" not in fake.verbs()


def test_setup_no_search_flag_does_not_ask_or_start(monkeypatch):
    terminal(monkeypatch, True)
    fake = FakeDocker(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--no-search must not prompt"))
    report = setup("--no-search")["search"]
    assert report["status"] == "not_running" and "--no-search" in report["note"]
    assert "run" not in fake.verbs()


def test_setup_with_the_engine_already_running_does_nothing(monkeypatch):
    terminal(monkeypatch, True)
    fake = FakeDocker(monkeypatch, up=True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("already running: nothing to ask"))
    assert setup("--search")["search"] == {"status": "running", "url": URL}
    assert fake.calls == []


def test_setup_search_needs_no_docker(monkeypatch):
    terminal(monkeypatch, True)
    FakeDocker(monkeypatch, installed=False)
    native = FakeNative(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--search must not prompt"))
    report = setup("--search")["search"]
    assert report["ok"] is True and report["engine"]["engine"] == "native" and native.installs == 1


def test_setup_json_shape_is_serialisable_and_failures_do_not_fail_setup(monkeypatch):
    terminal(monkeypatch, False)
    FakeDocker(monkeypatch)
    FakeNative(monkeypatch, fail="boom")
    result = setup("--search")
    assert json.loads(json.dumps(result))["search"] == {"ok": False, "error": "boom"}
    assert "skill" in result and "chromium" in result


def test_setup_and_search_engine_arguments():
    assert parse_args(["setup", "--search"]).search is True
    assert parse_args(["setup", "--no-search"]).no_search is True
    assert parse_args(["setup"]).search is False
    with pytest.raises(SystemExit):
        parse_args(["setup", "--search", "--no-search"])
    with pytest.raises(SystemExit):
        parse_args(["read", "https://example.com", "--search"])
    assert parse_args(["search-engine", "status"]).urls == ["status"]
    for bad in ([], ["restart"], ["start", "now"]):
        with pytest.raises(SystemExit):
            parse_args(["search-engine", *bad])


async def test_search_engine_subcommand_prints_json_only(monkeypatch, capsys):
    FakeDocker(monkeypatch, up=True)
    await cli.run(parse_args(["search-engine", "status"]))
    assert json.loads(capsys.readouterr().out) == {"status": "running", "url": URL}


# --- the receipt -----------------------------------------------------------------------------

def searxng_handler(up):
    def handle(request):
        if request.url.port == 8088:
            if not up:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"results": [
                {"url": "https://rust%d.test/async/%d" % (index, index), "title": "rust async story",
                 "content": "rust async"} for index in range(6)]})
        raise httpx.ConnectError("offline", request=request)
    return httpx.MockTransport(handle)


async def routing(tmp_path, up, **kwargs):
    async with Runtime(tmp_path, transport=searxng_handler(up)) as web:
        result = await web.search("rust async", policy=WebPolicy(provider="http"), **kwargs)
    return result["receipt"]["search_routing"]


async def test_receipt_says_how_to_install_local_search_when_there_is_none(tmp_path):
    note = (await routing(tmp_path, up=False))["local_search"]
    assert note["status"] == "not_installed"
    assert "SearXNG" in note["note"] and "thinner" in note["note"] and "no Docker" in note["note"]
    assert note["install_with"] == "frankensurf install search"


async def test_receipt_says_local_search_was_not_running(monkeypatch, tmp_path):
    FakeNative(monkeypatch, installed=True)
    note = (await routing(tmp_path, up=False))["local_search"]
    assert note["status"] == "not_running"
    assert "SearXNG" in note["note"] and "thinner" in note["note"]
    assert note["start_with"] == "frankensurf search-engine start"


async def test_receipt_has_no_note_when_the_engine_answered(tmp_path):
    assert "local_search" not in await routing(tmp_path, up=True)


async def test_receipt_note_is_left_out_of_explicit_and_custom_endpoint_searches(tmp_path):
    assert "local_search" not in await routing(tmp_path, up=False, source="searxng")
    assert "local_search" not in await routing(tmp_path, up=False,
                                               engine_config={"base_url": "http://127.0.0.1:9999"})


async def test_the_default_url_needs_no_environment_variable(monkeypatch):
    from frankensurf.search import REGISTRY, build_search
    monkeypatch.delenv("FRANKENSURF_SEARCH_URL", raising=False)
    assert REGISTRY["searxng"]["base_url"] == URL
    assert build_search("rust async", "searxng")[0].startswith(URL + "/search?")
