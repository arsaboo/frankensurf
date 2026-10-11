"""Find the page when the URL is wrong: a not-found read searches the site itself."""
import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

HOME = ("<html><title>Lamp shop</title><body><form action='/search' method='get' role='search'>"
        "<input type='search' name='q'></form>" + "<p>Lamps, shades and bulbs for every room.</p>" * 20
        + "</body></html>")
RESULTS = ("<html><title>Search: brass desk lamp</title><body>"
           + "".join(f'<a href="/product/brass-desk-lamp-{n}/SKU{n:06d}">Brass desk lamp {n} $49</a>' for n in range(12))
           + "</body></html>")
OFF = ("<html><title>Search</title><body>"
       + "".join(f'<a href="/product/garden-chair-{n}/SKU{n:06d}">Garden chair {n} $49</a>' for n in range(12))
       + "</body></html>")
QUIET = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0, "second_opinion_text_chars": 0,
         "hedge_after_seconds": 0, "warm_up_on_wall": False}


def site(results):
    class Plugin:
        manifest = ProviderManifest("http", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            pages = {"https://shop.example.com/": HOME,
                     "https://shop.example.com/search?q=brass+desk+lamp": results}
            if request.url not in pages:
                raise WebFailure("NOT_FOUND", "fixture 404", 404)
            return {"url": request.url, "content": pages[request.url], "content_type": "text/html",
                    "http_status": 200}
    return Plugin()


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(Runtime, "completeness_enabled", True)

    def go(plugin):
        registry = ProviderRegistry()
        registry.register(plugin)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return go


async def test_a_wrong_url_is_found_through_the_sites_own_search(tmp_path, install):
    install(site(RESULTS))
    url = "https://shop.example.com/p/brass-desk-lamp-123456"
    async with Runtime(tmp_path) as web:
        result = await web.read(url, policy_overrides=QUIET)
        plain = await web.read(url, policy_overrides={**QUIET, "rescue_not_found": False})
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and "Brass desk lamp 7" in result["text"]
    assert receipt["rescued"]["from"] == url and receipt["rescued"]["query"] == "brass desk lamp"
    assert receipt["rescued"]["via"] == "form" and receipt["next_step"]["reason"] == "rescued"
    assert "rescued" in receipt["quality"]["flags"]
    assert plain["receipt"]["failure"]["code"] == "NOT_FOUND"


async def test_results_that_ignore_the_words_are_not_a_rescue(tmp_path, install):
    install(site(OFF))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example.com/p/brass-desk-lamp-123456", policy_overrides=QUIET)
    receipt = result["receipt"]
    assert receipt["failure"]["code"] == "NOT_FOUND"
    assert receipt["rescue"]["found"] is False and receipt["rescue"]["query"] == "brass desk lamp"
    with pytest.raises(ValueError):
        WebPolicy(rescue_not_found="yes")


async def test_a_rescue_never_follows_a_search_on_another_site_and_keeps_its_costs(tmp_path, install,
                                                                                  monkeypatch):
    elsewhere = HOME.replace("action='/search'", "action='https://search.other.example/find'")

    class Plugin:
        manifest = ProviderManifest("http", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            if request.url == "https://shop.example.com/":
                return {"url": request.url, "content": elsewhere, "content_type": "text/html", "http_status": 200}
            if request.url.startswith("https://search.other.example/"):
                return {"url": request.url, "content": RESULTS, "content_type": "text/html", "http_status": 200}
            raise WebFailure("NOT_FOUND", "fixture 404", 404)
    install(Plugin())

    async def typed_search(self, home, words, effective):  # the search box navigates off the site
        return "https://search.other.example/find?q=brass+desk+lamp"
    monkeypatch.setattr(Runtime, "_probe_search", typed_search)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example.com/p/brass-desk-lamp-123456", policy_overrides=QUIET)
    receipt = result["receipt"]
    assert receipt["failure"]["code"] == "NOT_FOUND" and receipt["rescue"]["found"] is False
    assert "search_url" not in receipt["rescue"] and receipt["cost_usd"] == 0


async def test_a_search_page_that_ignores_its_query_is_rescued_too(tmp_path, install):
    home = HOME.replace("action='/search'", "action='/find'").replace("name='q'", "name='term'")

    class Plugin:
        manifest = ProviderManifest("http", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            pages = {"https://shop.example.com/": home,
                     "https://shop.example.com/search?q=brass+desk+lamp": OFF,
                     "https://shop.example.com/find?term=brass+desk+lamp": RESULTS}
            if request.url not in pages:
                raise WebFailure("NOT_FOUND", "fixture 404", 404)
            return {"url": request.url, "content": pages[request.url], "content_type": "text/html",
                    "http_status": 200}
    install(Plugin())
    url = "https://shop.example.com/search?q=brass+desk+lamp"
    async with Runtime(tmp_path) as web:
        result = await web.read(url, policy_overrides=QUIET)
    receipt = result["receipt"]
    assert "Brass desk lamp 3" in result["text"]
    assert receipt["rescued"]["after"] == "off_query" and receipt["rescued"]["via"] == "form"
    assert "ignored its search" in receipt["next_step"]["how"]


def test_url_words():
    from frankensurf.completeness import url_words
    assert url_words("https://www.seat61.com/trains-and-routes/london-to-paris-by-train.htm") == "london paris train"
    assert url_words("https://shop.example/search?q=air+fryer&page=2") == "air fryer"
    assert url_words("https://x.example/en/products/123456") == ""
    assert url_words("https://www.shop.example/productsearch.aspx?t=probiotic") == "probiotic"
    assert url_words("https://www.shop.example/busqueda-generica?busqueda=quijote") == "quijote"
    assert url_words("https://x.example/list?sort=price&view=grid") == ""
