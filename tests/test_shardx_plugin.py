"""The optional ShardX plugin (contrib/frankensurf-shardx), with ShardX and Playwright faked:
it runs only once trusted, starts one browser per session on first use, closes it with the
session, and reports page walls the way Core's own browsers do. The real ShardX never runs here;
.github/workflows/shardbench.yml checks it on a throwaway machine."""
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from frankensurf import Runtime, plugin_admin
from frankensurf.plugin_catalog import ENTRY_POINT_GROUPS, build_plugin_catalog

PLUGIN = Path(__file__).resolve().parents[1] / "contrib/frankensurf-shardx/src"
PAGE = "<html><head><title>Lamp</title></head><body>" + "<p>A brass desk lamp, $49.</p>" * 20 + "</body></html>"


class EntryPoint:
    def __init__(self, value):
        self.group = ENTRY_POINT_GROUPS["provider"]
        self.name = "shardx"
        self.dist = SimpleNamespace(name="frankensurf-shardx", version="0.1.0")
        self.value = value

    def load(self):
        return self.value


@pytest.fixture
def world(monkeypatch):
    """Fake shardx and playwright modules that record what the plugin does."""
    state = SimpleNamespace(launches=[], visits=[], pages_closed=0, stopped=0, browser_closed=0, status=200)

    class Page:
        url = ""

        async def goto(self, url, **options):
            state.visits.append(url)
            self.url = url
            return SimpleNamespace(status=state.status, headers={"content-type": "text/html"})

        async def wait_for_timeout(self, ms):
            pass

        async def content(self):
            return PAGE

        async def close(self):
            state.pages_closed += 1

    class Context:
        async def new_page(self):
            return Page()

    class Browser:
        contexts = [Context()]
        connected = True

        def is_connected(self):
            return self.connected

        async def close(self):
            self.connected = False
            state.browser_closed += 1

    class Chromium:
        async def connect_over_cdp(self, url):
            assert url == "ws://127.0.0.1:9222/devtools/browser/fake"
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            pass

    class Starter:
        async def start(self):
            return Playwright()

    class Session:
        cdp_url = "ws://127.0.0.1:9222/devtools/browser/fake"

        def stop(self):
            state.stopped += 1

    class Profile:
        def with_override(self, **options):
            return self

    class ShardX:
        def create_profile(self, platform):
            return Profile()

        def launch(self, profile, **options):
            state.launches.append(options)
            return Session()

    shardx = ModuleType("shardx")
    shardx.ShardX = ShardX
    playwright = ModuleType("playwright")
    async_api = ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: Starter()
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "shardx", shardx)
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setenv("FRANKENSURF_SHARDX_HEADLESS", "1")
    monkeypatch.syspath_prepend(str(PLUGIN))
    sys.modules.pop("frankensurf_shardx", None)
    import frankensurf_shardx
    state.entry_points = [EntryPoint(frankensurf_shardx.ShardXBrowser)]
    return state


def trusted(tmp_path, world):
    trust = tmp_path / "config" / "plugins.json"
    plugin_admin.change("trust", "provider", "shardx", "frankensurf-shardx", path=trust)
    return build_plugin_catalog(config_path=trust, entry_points=world.entry_points)


def test_it_loads_only_once_trusted(tmp_path, world):
    trust = tmp_path / "config" / "plugins.json"
    before = plugin_admin.status(trust, entry_points=world.entry_points)
    assert before["loaded"] == [] and before["rejected"][0]["code"] == "PLUGIN_NOT_TRUSTED"
    assert before["rejected"][0]["hint"].endswith("frankensurf plugins trust provider shardx frankensurf-shardx")
    plugin_admin.change("trust", "provider", "shardx", "frankensurf-shardx", path=trust)
    after = plugin_admin.status(trust, entry_points=world.entry_points)
    assert [item["id"] for item in after["loaded"]] == ["shardx"] and after["rejected"] == []


async def test_one_browser_per_session_closed_with_it(tmp_path, world):
    async with Runtime(tmp_path / "state", plugin_catalog=trusted(tmp_path, world)) as web:
        assert world.launches == []  # nothing starts until a read routes to it
        first = await web.read("https://shop.example.com/lamp", provider="shardx")
        second = await web.read("https://shop.example.com/lamp-2", provider="shardx")
    for result in (first, second):
        assert result["receipt"]["status"] == "observed" and result["receipt"]["method"] == "shardx"
        assert "brass desk lamp" in result["text"]
    assert len(world.launches) == 1 and world.launches[0]["cdp"] is True and world.launches[0]["headless"] is True
    assert world.visits == ["https://shop.example.com/lamp", "https://shop.example.com/lamp-2"]
    assert world.pages_closed == 2 and world.browser_closed == 1 and world.stopped == 1


async def test_a_wall_is_a_failure_not_a_page(tmp_path, world):
    world.status = 403
    async with Runtime(tmp_path / "state", plugin_catalog=trusted(tmp_path, world)) as web:
        result = await web.read("https://shop.example.com/lamp", provider="shardx")
    receipt = result["receipt"]
    assert receipt["status"] == "failed" and "BLOCKED" in str(receipt)
    assert world.pages_closed == 1


def test_unavailable_without_shardx(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "shardx", None)
    import frankensurf_shardx
    assert frankensurf_shardx.ShardXBrowser().available(None) is False
