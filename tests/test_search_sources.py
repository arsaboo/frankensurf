"""Firecrawl search, Exa options and dates, published dates on free sources, site_watch."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from frankensurf import Runtime, WebPolicy, hosted_providers
from frankensurf.hosted_providers import ExaTransport, FirecrawlSearchTransport, search_url
from frankensurf.providers import ProviderRequest
from frankensurf.runtime import WebFailure
from frankensurf.search import normalize_results
from frankensurf.search_plugins import DEFAULT_SEARCHES, HostedSearchPlugin
from frankensurf.site_watch import infer_site, key_terms

KEY = "test-key-must-not-leak"


@pytest.fixture
def api(monkeypatch):
    calls, replies = [], []

    async def call(method, url, policy, *, headers=None, params=None, json_body=None):
        calls.append({"method": method, "url": url, "headers": headers or {}, "json": json_body})
        status, body = replies.pop(0)
        return status, {}, json.dumps(body).encode()

    monkeypatch.setattr(hosted_providers, "_call", call)
    return calls, replies


# ---- published dates on free sources ----

def test_free_sources_surface_iso_published_dates():
    news = ('<rss xmlns:News="https://www.bing.com/news"><channel><item><title>Lomo</title>'
            '<link>https://news.example/a</link><pubDate>Thu, 08 Oct 2026 03:00:00 GMT</pubDate>'
            '</item></channel></rss>')
    rows, _ = normalize_results(news, None, "bing_news_rss", 5)
    assert rows[0]["published"] == "2026-10-08T03:00:00Z"
    rows, _ = normalize_results("", {"hits": [{"objectID": "1", "title": "T", "created_at": "2026-10-01T10:00:00.000Z"}]},
                                "hacker_news", 5)
    assert rows[0]["published"] == "2026-10-01T10:00:00Z"
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id><title>P</title>'
            '<summary>S</summary><published>2026-09-30T12:00:00Z</published></entry></feed>')
    rows, _ = normalize_results(atom, None, "arxiv", 5)
    assert rows[0]["published"] == "2026-09-30T12:00:00Z"
    repo = {"items": [{"html_url": "https://github.com/a/b", "full_name": "a/b",
                       "pushed_at": "2026-10-05T00:00:00Z", "created_at": "2020-01-01T00:00:00Z"}]}
    rows, _ = normalize_results("", repo, "github", 5)
    assert rows[0]["published"] == "2026-10-05T00:00:00Z"
    assert rows[0]["extra"]["created_at"] == "2020-01-01T00:00:00Z"
    stack = {"items": [{"link": "https://stackoverflow.com/q/1", "title": "Q", "creation_date": 1790000000,
                        "last_activity_date": 1790100000}]}
    rows, _ = normalize_results("", stack, "stack_exchange", 5)
    assert rows[0]["published"] == datetime.fromtimestamp(1790000000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Undated results carry no published key at all.
    rows, _ = normalize_results("", {"docs": [{"key": "/works/1", "title": "B"}]}, "open_library", 5)
    assert "published" not in rows[0]


# ---- Exa ----

async def test_exa_maps_recency_and_domains_natively_and_surfaces_published(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_EXA_API_KEY", KEY)
    replies.append((200, {"results": [{"url": "https://a.example/x", "title": "X", "publishedDate": "2026-10-02T00:00:00.000Z",
                                       "highlights": ["h"]}], "costDollars": {"total": 0.005}}))
    url = search_url("exa_api", "q", 5, {"site": "a.example", "exclude_domains": ["b.example"], "recency": "week"})
    result = await ExaTransport().acquire(ProviderRequest(url, WebPolicy()), None)
    body = calls[0]["json"]
    assert body["includeDomains"] == ["a.example"] and body["excludeDomains"] == ["b.example"]
    start = datetime.strptime(body["startPublishedDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - start) - timedelta(days=7)) < timedelta(minutes=2)
    assert result["cost_usd"] == 0.005
    rows = HostedSearchPlugin("exa", "exa_api", "exa_api_key")._normalize(json.loads(result["content"]), 5)
    assert rows[0]["published"] == "2026-10-02T00:00:00Z" and rows[0]["indexed_date"] == "2026-10-02T00:00:00.000Z"


# ---- Firecrawl search ----

def test_firecrawl_search_is_registered_paid_and_opt_in(monkeypatch):
    manifest = next(item for item in DEFAULT_SEARCHES.inspect() if item["id"] == "firecrawl_search")
    assert manifest["paid"] and manifest["vertical"] == "web" and manifest["transport_provider"] == "firecrawl_search_api"
    plugin = DEFAULT_SEARCHES._plugins["firecrawl_search"]
    assert plugin.available() is False
    monkeypatch.setenv("FRANKENSURF_FIRECRAWL_API_KEY", KEY)
    assert plugin.available() is True
    assert "firecrawl_search" not in DEFAULT_SEARCHES.candidates(WebPolicy())
    assert "firecrawl_search" in DEFAULT_SEARCHES.candidates(WebPolicy(allow_paid_fallbacks=True))


async def test_firecrawl_search_request_shape_cost_and_key_secrecy(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_FIRECRAWL_API_KEY", KEY)
    replies += [(200, {"success": True, "data": []})] * 4
    url = search_url("firecrawl_search_api", "film cameras", 10,
                     {"site": "a.example", "exclude_domains": ["b.example"], "recency": "month", "region": "en-AU"})
    result = await FirecrawlSearchTransport().acquire(ProviderRequest(url, WebPolicy()), None)
    assert calls[0]["method"] == "POST" and calls[0]["url"] == "https://api.firecrawl.dev/v2/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer " + KEY
    assert calls[0]["json"] == {"query": "film cameras site:a.example -site:b.example", "limit": 10,
                                "tbs": "qdr:m", "country": "AU"}
    assert result["cost_usd"] == pytest.approx(0.0076)  # 2 credits per 10 results at $0.0038
    for recency, tbs in (("day", "qdr:d"), ("week", "qdr:w"), ("year", "qdr:y")):
        await FirecrawlSearchTransport().acquire(
            ProviderRequest(search_url("firecrawl_search_api", "q", 20, {"recency": recency}), WebPolicy()), None)
        assert calls[-1]["json"]["tbs"] == tbs
    assert calls[-1]["json"]["limit"] == 20 and "tbs" not in FirecrawlSearchTransport._body("q", 5, {})
    assert KEY not in json.dumps(result, default=str)


async def test_firecrawl_search_cost_override_and_service_errors(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_FIRECRAWL_API_KEY", KEY)
    monkeypatch.setenv("FRANKENSURF_FIRECRAWL_SEARCH_COST_PER_QUERY", "0.003")
    replies += [(200, {"data": []}), (402, {}), (429, {})]
    url = search_url("firecrawl_search_api", "q", 5)
    assert (await FirecrawlSearchTransport().acquire(ProviderRequest(url, WebPolicy()), None))["cost_usd"] == 0.003
    for code in ("BUDGET_EXHAUSTED", "RATE_LIMITED"):
        with pytest.raises(WebFailure) as failure:
            await FirecrawlSearchTransport().acquire(ProviderRequest(url, WebPolicy()), None)
        assert failure.value.code == code


def test_firecrawl_search_normalizes_v1_list_and_v2_groups():
    plugin = HostedSearchPlugin("firecrawl_search", "firecrawl_search_api", "firecrawl_api_key")
    v1 = {"success": True, "data": [{"url": "https://a.example/1", "title": "One", "description": "d1",
                                     "metadata": {"publishedTime": "2026-10-01T08:00:00+02:00"}}]}
    assert plugin._normalize(v1, 5) == [{
        "url": "https://a.example/1", "title": "One", "snippet": "d1", "engines": ["firecrawl_search"],
        "indexed_date": "2026-10-01T08:00:00+02:00", "published": "2026-10-01T06:00:00Z"}]
    v2 = {"data": {"web": [{"url": "https://a.example/2", "title": "Two", "description": "d2"}],
                   "news": [{"url": "https://n.example/3", "title": "Three", "snippet": "s", "date": "2 days ago"},
                            {"title": "no url"}]}}
    rows = plugin._normalize(v2, 5)
    assert [row["url"] for row in rows] == ["https://a.example/2", "https://n.example/3"]
    assert all("published" not in row for row in rows)  # undated, or a date that is not a date
    assert len(plugin._normalize(v2, 1)) == 1
    with pytest.raises(ValueError):
        plugin._normalize({"data": "nope"}, 5)


async def test_firecrawl_search_end_to_end_through_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_FIRECRAWL_API_KEY", KEY)
    seen = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://a.example/1", "title": "One",
                                                   "description": "d", "metadata": {"publishedTime": today}}]})
    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(handle))
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(500))) as web:
        denied = await web.search("q", source="firecrawl_search")
        result = await web.search("q", source="firecrawl_search", recency="week",
                                  policy=WebPolicy(allow_paid_fallbacks=True))
    assert denied["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert result["receipt"]["status"] == "observed" and result["receipt"]["cost_usd"] == pytest.approx(0.0152)  # recency overfetches to 20 results
    assert result["results"][0]["published"] == today + "T00:00:00Z"
    assert json.loads(seen[0].content)["tbs"] == "qdr:w"
    assert KEY not in json.dumps(result, default=str)


# ---- site_watch ----

HOME = ('<html><head><title>Example</title><link rel="alternate" type="application/rss+xml" href="/blog/rss.xml">'
        '</head><body>' + "<p>Welcome</p>" * 30 + "</body></html>")


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate>"
                   f"<description>&lt;p&gt;{s}&lt;/p&gt;</description></item>" for t, u, d, s in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>{body}</channel></rss>'


def recent(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%a, %d %b %Y %H:%M:%S GMT")


def site(routes, log):
    def handle(request):
        log.append(str(request.url))
        reply = routes.get(request.url.path)
        if reply is None:
            return httpx.Response(404, text="missing", headers={"content-type": "text/plain"})
        content_type, text = reply
        return httpx.Response(200, text=text, headers={"content-type": content_type})
    return httpx.MockTransport(handle)


def test_site_inference_and_key_terms():
    assert infer_site("example.com pricing change") == ("example.com", "pricing change")
    assert infer_site("new releases https://www.Example.org/blog") == ("example.org", "new releases")
    assert infer_site("anything site:docs.example.co.uk apis") == ("docs.example.co.uk", "anything apis")
    for query in ("node.js release notes", "report.pdf summary", "e.g. things", "just words", "v1.2 changes"):
        assert infer_site(query)[0] is None
    assert key_terms("the latest pricing updates for Widgets") == ["pricing", "widgets"]


async def test_site_watch_reads_advertised_feed_filters_and_sorts_newest_first(tmp_path):
    log = []
    feed = rss(("Old widget news", "https://example.com/blog/old", recent(60), "ancient"),
               ("Widget launch", "https://example.com/blog/launch", recent(3), "We launched &lt;b&gt;widgets&lt;/b&gt;"),
               ("Unrelated", "https://example.com/blog/other", recent(2), "nothing"),
               ("Widget update", "https://example.com/blog/update", recent(1), "More"),
               ("Elsewhere widget", "https://elsewhere.net/widget", recent(1), "off site"))
    transport = site({"/": ("text/html", HOME), "/blog/rss.xml": ("application/rss+xml", feed)}, log)
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("example.com widget", source="site_watch", recency="month")
    assert result["receipt"]["status"] == "observed", result["receipt"]
    assert [item["url"] for item in result["results"]] == [
        "https://example.com/blog/update", "https://example.com/blog/launch"]
    first = result["results"][1]
    assert first["snippet"] == "We launched widgets" and first["engines"] == ["site_watch"]
    assert first["published"].endswith("Z") and first["indexed_date"] == first["published"]
    assert first["extra"] == {"discovered_via": "feed"}
    assert result["source"] == "site_watch" and result["receipt"]["cost_usd"] == 0
    assert len(log) == 2  # homepage + the advertised feed; nothing else probed
    assert result["receipt"]["search_options"]["recency"] == "month"


async def test_site_watch_site_option_and_vertical_routing(tmp_path):
    feed = rss(("Release 2", "https://example.com/r2", recent(1), ""), ("Release 1", "https://example.com/r1", recent(5), ""))
    transport = site({"/": ("text/html", HOME), "/blog/rss.xml": ("application/rss+xml", feed)}, [])
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("release", vertical="site", site="example.com")
        assert result["source"] == "site_watch"
        assert [item["title"] for item in result["results"]] == ["Release 2", "Release 1"]
        assert result["receipt"]["search_routing"]["candidates"][0] == "site_watch"  # web engines are later rungs


async def test_site_watch_probes_conventional_feeds_then_stops_at_first_hit(tmp_path):
    log = []
    feed = rss(("Hello", "https://example.com/hello", recent(1), "hi"))
    transport = site({"/": ("text/html", "<html><body>" + "x" * 300 + "</body></html>"),
                      "/rss": ("application/rss+xml", feed), "/rss.xml": ("application/rss+xml", feed)}, log)
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("example.com", source="site_watch")
    assert [item["url"] for item in result["results"]] == ["https://example.com/hello"]
    assert [urlparse(url).path for url in log] == ["/", "/feed", "/rss"]


SITEMAP_INDEX = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 '<sitemap><loc>https://example.com/sitemap-pages.xml</loc><lastmod>2020-01-01</lastmod></sitemap>'
                 '<sitemap><loc>https://example.com/sitemap-posts-2026.xml</loc><lastmod>2026-10-08</lastmod></sitemap>'
                 '</sitemapindex>')


def urlset(*pairs):
    rows = "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in pairs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'


async def test_site_watch_falls_back_to_robots_sitemap_index_and_child_sitemap(tmp_path):
    log = []
    today = datetime.now(timezone.utc)
    day = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d")
    posts = urlset(("https://example.com/posts/pricing-update", day(1)),
                   ("https://example.com/posts/team-offsite", day(2)),
                   ("https://example.com/posts/ancient-pricing", "2019-03-04"))
    transport = site({
        "/": ("text/html", "<html><body>" + "x" * 300 + "</body></html>"),
        "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sitemap_index.xml\n"),
        "/sitemap_index.xml": ("application/xml", SITEMAP_INDEX),
        "/sitemap-posts-2026.xml": ("application/xml", posts),
    }, log)
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("example.com pricing", source="site_watch", recency="month")
    assert result["receipt"]["status"] == "observed", result["receipt"]
    assert [item["url"] for item in result["results"]] == ["https://example.com/posts/pricing-update"]
    item = result["results"][0]
    assert item["title"] == "Pricing update" and item["extra"] == {"discovered_via": "sitemap"}
    assert item["published"] == day(1) + "T00:00:00Z"
    paths = [urlparse(url).path for url in log]
    assert paths[:2] == ["/", "/feed"] and "/sitemap-posts-2026.xml" in paths and len(paths) <= 8


async def test_site_watch_news_sitemap_dates_and_titles(tmp_path):
    news = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://example.com/a</loc>'
            '<news:news><news:publication_date>2026-10-07T09:00:00Z</news:publication_date>'
            '<news:title>Big story</news:title></news:news></url></urlset>')
    transport = site({"/": ("text/html", "<html><body>" + "x" * 300 + "</body></html>"),
                      "/sitemap.xml": ("application/xml", news)}, [])
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("example.com", source="site_watch")
    assert (result["results"][0]["title"], result["results"][0]["published"]) == ("Big story", "2026-10-07T09:00:00Z")


async def test_site_watch_fails_fast_without_a_site_and_when_nothing_is_published(tmp_path):
    log = []
    async with Runtime(tmp_path, transport=site({"/": ("text/html", "<html><body>" + "x" * 300 + "</body></html>")}, log)) as web:
        nosite = await web.search("just some words", source="site_watch")
        assert log == []
        assert nosite["receipt"]["failure"]["code"] == "INVALID_URL"
        assert "needs a site" in nosite["receipt"]["failure"]["message"]
        bare = await web.search("example.com", source="site_watch")
    assert bare["receipt"]["status"] == "failed"
    assert "No feed or sitemap found for example.com" in bare["receipt"]["failure"]["message"]
    assert len(log) <= 8


def test_site_watch_is_a_free_site_vertical_source():
    manifest = next(item for item in DEFAULT_SEARCHES.inspect() if item["id"] == "site_watch")
    assert manifest["vertical"] == "site" and not manifest["paid"] and manifest["transport_provider"] == "http"


def test_arxiv_query_prefixes_every_topic_word():
    from frankensurf.search import build_search
    url, _ = build_search("recent papers on mixture of experts", "arxiv", {"recency": "year"})
    assert "all%3Amixture%20AND%20all%3Aexperts" in url
    assert "recent" not in url.split("search_query=")[1].split("&")[0]
