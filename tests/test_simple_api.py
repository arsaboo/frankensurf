"""frankensurf.read / batch / search: the short way in, sync and async (synthetic providers)."""
import asyncio

import pytest

import frankensurf as fs
from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import WebFailure

PAGE = ('<html><title>Pricing</title><body>' + "<p>Plans and prices for the product.</p>" * 30 +
        '<a href="/plans/pro">Pro plan</a></body></html>')
POLICY = {"origin_route_hint_ttl_seconds": 0, "second_opinion_text_chars": 0, "origin_min_interval_seconds": 0}


@pytest.fixture
def install(monkeypatch):
    def apply(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return apply


def plugin(identifier="fixture", failure=None):
    class Plugin:
        seen = []

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            type(self).seen.append(request.url)
            if failure or "walled" in request.url:
                raise WebFailure(failure or "CAPTCHA", "fixture")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


def test_read_returns_a_page_with_shortcuts(tmp_path, install):
    install(plugin())
    page = fs.read("https://shop.example/pricing", state_dir=str(tmp_path), links=True, **POLICY)
    assert isinstance(page, dict) and page.ok and page.failure is None
    assert page.title == "Pricing" and "Plans and prices" in page.text
    assert page.via == "fixture" and page.cost_usd == 0 and page.trace_id
    assert page.links == [{"url": "https://shop.example/plans/pro", "text": "Pro plan"}]


def test_a_page_no_tool_could_read_says_why(tmp_path, install):
    install(plugin(failure="CAPTCHA"))
    page = fs.read("https://shop.example/pricing", state_dir=str(tmp_path), **POLICY)
    assert not page.ok and page.failure["code"] == "CAPTCHA" and page.text == ""


def test_batch_keeps_order_and_reports_each_page(tmp_path, install):
    install(plugin())
    pages = fs.batch(["https://a.example/1", "https://walled.example/2", "https://c.example/3"],
                     state_dir=str(tmp_path), **POLICY)
    assert [page.url for page in pages] == ["https://a.example/1", "https://walled.example/2",
                                            "https://c.example/3"]
    assert [page.ok for page in pages] == [True, False, True]


def test_sync_read_works_inside_a_running_event_loop(tmp_path, install):
    install(plugin())

    async def caller():
        return fs.read("https://shop.example/pricing", state_dir=str(tmp_path), **POLICY)
    assert asyncio.run(caller()).ok


async def test_async_read(tmp_path, install):
    install(plugin())
    page = await fs.aread("https://shop.example/pricing", state_dir=str(tmp_path), **POLICY)
    assert page.ok


def test_options_become_policy_settings(monkeypatch):
    from frankensurf import simple
    assert simple._overrides(True, 0.02, True, True, {"lightning": True}) == {
        "lightning": True, "allow_paid_fallbacks": True, "max_cost_usd": 0.02,
        "main_content": True, "include_links": True}
    assert simple._overrides(False, None, False, False, {}) == {}


def test_the_search_submodule_does_not_replace_search():
    import importlib
    import frankensurf.search  # noqa: F401  (the submodule, imported the usual way)
    importlib.import_module("frankensurf.search")
    assert callable(fs.search) and fs.search is fs.simple.search


def test_search_returns_plain_results(tmp_path, monkeypatch):
    from frankensurf.runtime import Runtime

    async def fake_search(self, query, **kwargs):
        assert kwargs["policy"].provider == "http" and kwargs["recency"] == "month"
        return {"results": [{"url": "https://a.example", "title": "A", "snippet": "about a",
                             "published": None, "engines": ["x"]}]}
    monkeypatch.setattr(Runtime, "search", fake_search)
    assert fs.search("anything", recency="month", state_dir=str(tmp_path)) == [
        {"url": "https://a.example", "title": "A", "snippet": "about a", "published": None}]
