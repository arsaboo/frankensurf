"""Search options (site, exclude_domains, recency, region) and vertical sources."""
from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from frankensurf import Runtime
from frankensurf.search import build_search, filter_results, normalize_options, normalize_results

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def params(url):
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


def test_options_are_validated():
    assert normalize_options(site="https://www.Example.com/x", exclude_domains=["a.com", "www.b.org", "a.com"],
                             recency="week", region="en-AU") == {
        "site": "example.com", "exclude_domains": ["a.com", "b.org"], "recency": "week", "region": "en-AU"}
    for bad in ({"recency": "fortnight"}, {"site": "not a domain"}, {"region": "australia"},
                {"exclude_domains": ["x.com"] * 201}):
        with pytest.raises(ValueError):
            normalize_options(**bad)


def test_each_web_source_applies_options_its_own_way():
    options = {"site": "example.com", "exclude_domains": ["spam.com", "junk.net"], "recency": "week",
               "region": "en-AU"}
    url, _ = build_search("film camera", "bing_rss", options, now=NOW)
    got = params(url)
    assert got["q"] == "film camera site:example.com -site:junk.net -site:spam.com"
    assert got["filters"] == 'ex1:"ez2"' and got["cc"] == "AU"
    got = params(build_search("film camera", "duckduckgo_html", options, now=NOW)[0])
    assert got["df"] == "w" and got["kl"] == "au-en" and "-site:spam.com" in got["q"]
    got = params(build_search("film camera", "searxng", options, now=NOW)[0])
    assert got["time_range"] == "week" and got["language"] == "en-AU"
    got = params(build_search("rust async", "hacker_news", {"recency": "day"}, now=NOW)[0])
    assert got["numericFilters"] == "created_at_i>%d" % (NOW.timestamp() - 86400)
    got = params(build_search("tokio", "github", {"recency": "month"}, now=NOW)[0])
    # A bare project name searches repository names; stars order the answer.
    assert got["q"] == "tokio in:name pushed:>2026-09-08" and got["sort"] == "stars"
    assert build_search("Leica", "wikipedia", {"region": "de"})[0].startswith("https://de.wikipedia.org/w/api.php?")


def test_filter_drops_excluded_off_site_and_old_results():
    results = [{"url": "https://spam.com/a", "indexed_date": None},
               {"url": "https://news.example.com/b", "indexed_date": "2026-10-08T00:00:00Z"},
               {"url": "https://example.com/c", "indexed_date": "Mon, 01 Jun 2026 00:00:00 GMT"},
               {"url": "https://example.com/d", "indexed_date": None},
               {"url": "https://other.org/e", "indexed_date": None}]
    kept, dropped = filter_results(results, {"site": "example.com", "exclude_domains": ["spam.com"],
                                             "recency": "week"}, now=NOW)
    assert [item["url"] for item in kept] == ["https://news.example.com/b", "https://example.com/d"]
    assert dropped == {"excluded": 1, "off_site": 1, "too_old": 1, "undated": 1}


def test_vertical_sources_normalize():
    news = ('<rss xmlns:News="https://www.bing.com/news"><channel><item><title>Lomo</title>'
            '<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnewatlas.com%2fa</link>'
            '<description>d</description><pubDate>Thu, 08 Oct 2026 09:28:33 GMT</pubDate>'
            '<News:Source>New Atlas</News:Source></item></channel></rss>')
    found, _ = normalize_results(news, None, "bing_news_rss", 10)
    assert found[0]["url"] == "https://newatlas.com/a" and found[0]["extra"]["publisher"] == "New Atlas"
    wiki = {"query": {"search": [{"title": "Film camera", "snippet": "A <span>camera</span>", "timestamp": "2026-01-01T00:00:00Z"}]}}
    found, _ = normalize_results("", wiki, "wikipedia", 10, "https://de.wikipedia.org/w/api.php?x=1")
    assert found[0]["url"] == "https://de.wikipedia.org/wiki/Film_camera" and found[0]["snippet"] == "A camera"
    hn = {"hits": [{"objectID": "42", "title": "Show HN", "url": None, "created_at": "2026-10-01T00:00:00Z",
                    "points": 10, "num_comments": 3}]}
    found, _ = normalize_results("", hn, "hacker_news", 10)
    assert found[0]["url"] == "https://news.ycombinator.com/item?id=42" and found[0]["extra"]["points"] == 10
    se = {"items": [{"link": "https://stackoverflow.com/q/1", "title": "How &amp; why", "tags": ["python"],
                     "is_answered": True, "score": 5, "last_activity_date": 1760000000}]}
    assert normalize_results("", se, "stack_exchange", 10)[0][0]["title"] == "How & why"
    gh = {"items": [{"html_url": "https://github.com/a/b", "full_name": "a/b", "description": "x",
                     "pushed_at": "2026-10-01T00:00:00Z", "stargazers_count": 7}]}
    assert normalize_results("", gh, "github", 10)[0][0]["extra"]["stars"] == 7
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2601.00001v1</id>'
            '<title>A  paper</title><summary> s </summary><published>2026-01-02T00:00:00Z</published></entry></feed>')
    assert normalize_results(atom, None, "arxiv", 10)[0][0]["title"] == "A paper"
    ol = {"docs": [{"key": "/works/OL1W", "title": "Film", "author_name": ["Ann"], "first_publish_year": 1999}]}
    assert normalize_results("", ol, "open_library", 10)[0][0]["snippet"] == "Ann · 1999"


async def test_router_routes_by_vertical_and_filters_results(tmp_path):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if "hn.algolia.com" in str(request.url):
            return httpx.Response(200, json={"hits": [
                {"objectID": "1", "title": "Keep rust", "url": "https://blog.example.com/a", "created_at": "2026-10-08T00:00:00Z"},
                {"objectID": "2", "title": "Drop rust", "url": "https://spam.com/b", "created_at": "2026-10-08T00:00:00Z"}]})
        return httpx.Response(500)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("rust", vertical="discussions", exclude_domains=["spam.com"], limit=5)
        with pytest.raises(ValueError):
            await web.search("rust", vertical="gossip")
    assert [item["url"] for item in result["results"]] == ["https://blog.example.com/a"]
    assert result["receipt"]["search_options"]["filtered"]["excluded"] == 1
    assert result["receipt"]["search_routing"]["vertical"] == "discussions"
    assert "hn.algolia.com" in seen[0]  # its own vertical first; the web engines are later rungs


def test_paid_apis_get_options_natively(monkeypatch):
    from frankensurf import hosted_providers
    url = hosted_providers.search_url("tavily_api", "film camera", 10,
                                      {"site": "example.com", "exclude_domains": ["a.com", "b.com"], "recency": "week"})
    options = hosted_providers._SearchTransport._options(url)
    assert options == {"site": "example.com", "recency": "week", "exclude_domains": ["a.com", "b.com"]}
    assert hosted_providers.TavilyTransport._tavily_body("q", 10, options) == {
        "query": "q", "max_results": 10, "include_domains": ["example.com"],
        "exclude_domains": ["a.com", "b.com"], "time_range": "week"}
    brave = hosted_providers.BraveTransport._brave_params("q", 10, {**options, "region": "en-AU"})
    assert brave["freshness"] == "pw" and brave["country"] == "au" and "-site:a.com" in brave["q"]


async def test_merge_mode_fuses_sources_and_survives_a_blocked_one(tmp_path):
    rss = ('<rss><channel>'
           '<item><title>Both lamp</title><link>https://www.shop.example/a?utm_source=x</link></item>'
           '<item><title>Bing only lamp</title><link>https://other.example/b</link></item></channel></rss>')
    searx = {"results": [{"url": "https://shop.example/a/", "title": "Both", "engines": ["google"]},
                         {"url": "https://third.example/c", "title": "Searx only"}]}

    def handle(request):
        url = str(request.url)
        if "bing.com" in url:
            return httpx.Response(200, text=rss, headers={"content-type": "application/rss+xml"})
        if "127.0.0.1" in url:
            return httpx.Response(200, json=searx)
        return httpx.Response(403, text="blocked")
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("lamp", mode="merge", limit=5)
    urls = [item["url"] for item in result["results"]]
    assert len(urls) == 3 and urls[0] in ("https://shop.example/a/", "https://www.shop.example/a?utm_source=x")
    top = result["results"][0]
    assert set(top["engines"]) == {"google", "bing"} and len(top["fusion"]["sources"]) == 2
    merge = {row["source"]: row for row in result["receipt"]["search_merge"]}
    assert merge["duckduckgo_html"]["status"] == "failed" and merge["bing_rss"]["results"] == 2
    assert result["receipt"]["status"] == "observed"
