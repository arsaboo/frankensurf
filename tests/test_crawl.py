"""map and crawl over a synthetic site served through a mock transport."""
import json

import httpx

from frankensurf.crawl import crawl, site_map
from frankensurf.runtime import Runtime

BODY = "<p>" + "Plenty of real words about this page. " * 20 + "</p>"
SITE = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\nSitemap: https://shop.test/sitemap.xml\n"),
    "/sitemap.xml": ("application/xml",
                     '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     "<sitemap><loc>https://shop.test/sitemap-pages.xml</loc></sitemap></sitemapindex>"),
    "/sitemap-pages.xml": ("application/xml",
                           '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           "<url><loc>https://shop.test/about</loc></url><url><loc>https://shop.test/logo.png</loc></url>"
                           "</urlset>"),
    "/": ("text/html", "<html><title>Home</title><body>" + BODY +
          '<a href="/lenses">Lenses</a><a href="/private/admin">Admin</a>'
          '<a href="https://elsewhere.test/x">Out</a><a href="/blog/post-1#top">Post</a></body></html>"'),
    "/lenses": ("text/html", "<html><title>Lenses</title><body><h1>Lenses</h1>" + BODY +
                '<a href="/lenses/50mm">50mm</a></body></html>'),
    "/lenses/50mm": ("text/html", "<html><title>50mm</title><body>" + BODY + '<a href="/deeper">Deeper</a></body></html>'),
    "/about": ("text/html", "<html><title>About</title><body>" + BODY + "</body></html>"),
    "/blog/post-1": ("text/html", "<html><title>Post</title><body>" + BODY + "</body></html>"),
}
POLICY = {"provider": "http", "origin_min_interval_seconds": 0, "origin_route_hint_ttl_seconds": 0,
          "second_opinion_text_chars": 0}


def transport(requested):
    def handle(request):
        requested.append(request.url.path)
        if request.url.host != "shop.test" or request.url.path not in SITE:
            return httpx.Response(404, text="nope")
        kind, body = SITE[request.url.path]
        return httpx.Response(200, text=body, headers={"content-type": kind})
    return httpx.MockTransport(handle)


async def test_map_lists_the_sites_urls_from_sitemaps_and_links(tmp_path):
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        found = await site_map(web, "https://shop.test/", policy_overrides=POLICY)
    assert found["links"][:1] == ["https://shop.test/about"]
    assert set(found["links"]) == {"https://shop.test/about", "https://shop.test/", "https://shop.test/lenses",
                                   "https://shop.test/private/admin", "https://shop.test/blog/post-1"}
    assert found["sources"] == {"sitemap": 2, "page": 4}  # four links on the page, one off-site
    async with Runtime(tmp_path / "b", transport=transport([]), domain_delay=0) as web:
        ranked = await site_map(web, "https://shop.test/", search="lens", policy_overrides=POLICY)
    assert ranked["links"] == ["https://shop.test/lenses"]


async def test_crawl_walks_breadth_first_within_depth_and_robots(tmp_path):
    requested = []
    async with Runtime(tmp_path, transport=transport(requested), domain_delay=0) as web:
        done = await crawl(web, "https://shop.test/", max_depth=2, policy_overrides=POLICY)
    urls = [page["url"] for page in done["pages"]]
    assert urls[0] == "https://shop.test/"
    assert set(urls) == {"https://shop.test/", "https://shop.test/about", "https://shop.test/lenses",
                         "https://shop.test/blog/post-1", "https://shop.test/lenses/50mm"}
    assert "/deeper" not in requested and "/private/admin" not in requested  # depth 3, and robots
    assert done["skipped"]["robots"] == 1 and done["read"] == 5
    lenses = next(page for page in done["pages"] if page["url"].endswith("/lenses"))
    assert lenses["markdown"].startswith("# Lenses") and lenses["status"] == "observed"


async def test_crawl_limit_include_exclude(tmp_path):
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        limited = await crawl(web, "https://shop.test/", limit=2, policy_overrides=POLICY)
    assert limited["completed"] == 2 and limited["left_in_queue"] > 0
    async with Runtime(tmp_path / "b", transport=transport([]), domain_delay=0) as web:
        only = await crawl(web, "https://shop.test/", include=[r"^/lenses"], use_sitemap=False, policy_overrides=POLICY)
    assert [page["url"] for page in only["pages"]] == [
        "https://shop.test/", "https://shop.test/lenses", "https://shop.test/lenses/50mm"]
    async with Runtime(tmp_path / "c", transport=transport([]), domain_delay=0) as web:
        without = await crawl(web, "https://shop.test/", exclude=[r"^/blog"], respect_robots=False,
                              use_sitemap=False, policy_overrides=POLICY)
    urls = [page["url"] for page in without["pages"]]
    assert "https://shop.test/blog/post-1" not in urls and "https://shop.test/private/admin" in urls


async def test_serve_map_and_a_crawl_job_in_firecrawls_shapes(tmp_path):
    import asyncio
    from frankensurf.firecrawl_compat import FirecrawlCompat
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        app = FirecrawlCompat(web, POLICY, base_url="http://127.0.0.1:3002")
        status, mapped = await app.handle("POST", "/v1/map", b'{"url": "https://shop.test/", "search": "lens"}')
        assert status == 200 and mapped == {"success": True, "links": ["https://shop.test/lenses"]}
        _, mapped2 = await app.handle("POST", "/v2/map", b'{"url": "https://shop.test/", "search": "lens"}')
        assert mapped2["links"] == [{"url": "https://shop.test/lenses"}]
        status, started = await app.handle("POST", "/v1/crawl",
                                           b'{"url": "https://shop.test/", "limit": 3, "maxDepth": 1}')
        assert status == 200 and started["url"] == "http://127.0.0.1:3002/v1/crawl/" + started["id"]
        for _ in range(100):
            _, job = await app.handle("GET", "/v1/crawl/" + started["id"], b"")
            if job["status"] != "scraping":
                break
            await asyncio.sleep(0.05)
    assert job["status"] == "completed" and job["completed"] == 3 and len(job["data"]) == 3
    assert job["data"][0]["metadata"]["sourceURL"] == "https://shop.test/"
    assert all(doc["markdown"] for doc in job["data"])


async def test_v2_crawl_with_object_formats_returns_markdown(tmp_path):
    import asyncio
    from frankensurf.firecrawl_compat import FirecrawlCompat
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        app = FirecrawlCompat(web, POLICY)
        _, started = await app.handle("POST", "/v2/crawl", json.dumps({
            "url": "https://shop.test/", "limit": 2, "scrapeOptions": {"formats": [{"type": "markdown"}]}}).encode())
        for _ in range(100):
            _, job = await app.handle("GET", "/v2/crawl/" + started["id"], b"")
            if job["status"] != "scraping":
                break
            await asyncio.sleep(0.05)
    assert job["status"] == "completed" and job["data"] and all(doc["markdown"] for doc in job["data"])


async def test_serve_batch_scrape_job(tmp_path):
    import asyncio
    from frankensurf.firecrawl_compat import FirecrawlCompat
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        app = FirecrawlCompat(web, POLICY)
        _, started = await app.handle("POST", "/v1/batch/scrape",
                                      b'{"urls": ["https://shop.test/lenses", "https://shop.test/missing"], "formats": ["markdown"]}')
        for _ in range(100):
            _, job = await app.handle("GET", "/v1/batch/scrape/" + started["id"], b"")
            if job["status"] != "scraping":
                break
            await asyncio.sleep(0.05)
    assert job["status"] == "completed" and job["completed"] == 2 and len(job["data"]) == 1
    assert job["data"][0]["markdown"].startswith("# Lenses")
    assert job["frankensurf"]["failed"][0]["url"] == "https://shop.test/missing"


def hooked_transport(hooks):
    base = transport([])

    def handle(request):
        if request.url.host == "hooks.test":
            hooks.append(json.loads(request.content))
            return httpx.Response(200, text="ok")
        return base.handle_request(request)
    return httpx.MockTransport(handle)


async def test_crawl_webhooks_and_jobs_that_outlive_the_server(tmp_path):
    import asyncio
    from frankensurf.firecrawl_compat import FirecrawlCompat
    hooks = []
    async with Runtime(tmp_path, transport=hooked_transport(hooks), domain_delay=0) as web:
        app = FirecrawlCompat(web, POLICY)
        body = json.dumps({"url": "https://shop.test/lenses", "limit": 5,
                           "webhook": {"url": "https://hooks.test/in", "metadata": {"run": 7},
                                       "events": ["started", "page", "completed"]}})
        _, started = await app.handle("POST", "/v1/crawl", body.encode())
        for _ in range(100):
            _, job = await app.handle("GET", "/v1/crawl/" + started["id"], b"")
            if job["status"] != "scraping":
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        # A fresh server (nothing in memory) still answers from disk.
        _, again = await FirecrawlCompat(web, POLICY).handle("GET", "/v1/crawl/" + started["id"], b"")
        jobs = web.state_dir / "serve-jobs"
        (jobs / ("crawl-" + "0" * 8 + "-0000-0000-0000-" + "0" * 12 + ".json")).write_text(
            json.dumps({"status": "scraping", "total": 0, "completed": 1, "data": [], "failed": []}))
        _, stuck = await FirecrawlCompat(web, POLICY).handle(
            "GET", "/v1/crawl/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, b"")
    assert job["status"] == "completed" and again["status"] == "completed" and len(again["data"]) == 2
    assert [hook["type"] for hook in hooks] == ["crawl.started", "crawl.page", "crawl.page", "crawl.completed"]
    assert hooks[0]["metadata"] == {"run": 7} and hooks[-1]["id"] == started["id"]
    assert stuck["status"] == "failed" and "stopped before" in stuck["error"]


async def test_change_tracking_new_same_changed(tmp_path, monkeypatch):
    from frankensurf.firecrawl_compat import FirecrawlCompat
    body = b'{"url": "https://shop.test/about", "formats": ["markdown", {"type": "changeTracking", "modes": ["git-diff"]}]}'
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        app = FirecrawlCompat(web, {**POLICY, "freshness": "now"})
        _, first = await app.handle("POST", "/v2/scrape", body)
        _, second = await app.handle("POST", "/v2/scrape", body)
        monkeypatch.setitem(SITE, "/about", ("text/html", "<html><title>About</title><body>" + BODY +
                                             "<p>We moved.</p></body></html>"))
        _, third = await app.handle("POST", "/v2/scrape", body)
    assert first["data"]["changeTracking"]["changeStatus"] == "new"
    assert second["data"]["changeTracking"]["changeStatus"] == "same"
    assert second["data"]["changeTracking"]["previousScrapeAt"]
    assert third["data"]["changeTracking"]["changeStatus"] == "changed"
    assert "+We moved." in third["data"]["changeTracking"]["diff"]["text"]


def test_scrape_options_become_frankensurf_settings():
    from frankensurf.firecrawl_compat import STEALTH_CANDIDATES, FirecrawlCompat, _screenshot_spec
    app = FirecrawlCompat(type("Web", (), {})(), {})
    settings = app._overrides({"location": {"country": "DE", "languages": ["de-DE", "en"]},
                               "actions": [{"type": "click", "selector": "#more"}],
                               "formats": [{"type": "screenshot", "fullPage": True}]})
    assert settings["accept_language"] == "de-DE,en;q=0.8"
    assert settings["provider"] == "local" and settings["screenshot_full_page"] is True
    assert settings["browser_actions"] == ({"type": "click", "selector": "#more"},)
    assert app._overrides({"proxy": "stealth"})["provider_candidates"] == STEALTH_CANDIDATES
    assert _screenshot_spec(["screenshot@fullPage"]) == {"fullPage": True} and _screenshot_spec(["markdown"]) is None


async def test_bad_actions_are_refused_before_reading(tmp_path):
    from frankensurf.firecrawl_compat import FirecrawlCompat
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        status, out = await FirecrawlCompat(web, POLICY).handle(
            "POST", "/v1/scrape", b'{"url": "https://shop.test/", "actions": [{"type": "launch_missiles"}]}')
    assert status == 400 and out["error"].startswith("actions:")


async def test_crawl_stays_under_the_start_path_unless_entire_domain(tmp_path):
    async with Runtime(tmp_path, transport=transport([]), domain_delay=0) as web:
        under = await crawl(web, "https://shop.test/lenses", policy_overrides=POLICY)
    assert [page["url"] for page in under["pages"]] == ["https://shop.test/lenses", "https://shop.test/lenses/50mm"]
    async with Runtime(tmp_path / "b", transport=transport([]), domain_delay=0) as web:
        everything = await crawl(web, "https://shop.test/lenses", entire_domain=True, use_sitemap=False,
                                 max_depth=1, policy_overrides=POLICY)
    assert [page["url"] for page in everything["pages"]] == ["https://shop.test/lenses", "https://shop.test/lenses/50mm"]
