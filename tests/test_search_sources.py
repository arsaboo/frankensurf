"""Boolean (monitoring-style) queries and the v0.41 search sources: Google News (free),
Serper, SerpAPI and Google Programmable Search (keyed). Synthetic fixtures only."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from frankensurf import hosted_providers
from frankensurf.runtime import Runtime, WebPolicy
from frankensurf.search import build_search, normalize_results
from frankensurf.search_checks import (boolean_groups, check_results, focus_query, infer_vertical, is_boolean,
                                       simple_query)

KEY = "test-key-must-not-leak"
ARGUS = '"Harbour Mining" OR "Harbour Mining Group" (acquisition OR layoffs OR "data breach")'


# --- reading a boolean query -----------------------------------------------------------------

def test_boolean_queries_are_recognised_and_taken_apart():
    assert is_boolean(ARGUS) and is_boolean("a | b") and is_boolean('"x y" "z w"')
    assert not is_boolean("harbour mining news") and not is_boolean('"Harbour Mining" results')
    assert boolean_groups(ARGUS) == [["Harbour Mining", "Harbour Mining Group"],
                                     ["acquisition", "layoffs", "data breach"]]
    assert boolean_groups("(python OR rust) web framework -java site:example.com") == [
        ["python", "rust"], ["web"], ["framework"]]


def test_the_checks_judge_a_boolean_query_by_its_name_and_the_simpler_query_is_the_name():
    assert focus_query(ARGUS) == "Harbour Mining | Harbour Mining Group"
    assert focus_query("plain words stay") == "plain words stay"
    assert simple_query(ARGUS) == '"Harbour Mining"'
    assert simple_query("plain words") is None


def test_a_story_naming_the_company_is_on_query_without_the_topic_words():
    items = [{"url": "https://news.example.com/2026/10/harbour-mining-expands-port", "title":
              "Harbour Mining expands its port operations", "snippet": "The miner said on Monday."},
             {"url": "https://news.example.com/2026/10/other-miner-profit", "title": "Another miner lifts profit",
              "snippet": "Results beat forecasts."}]
    verdicts = {item["url"]: item for item in check_results(focus_query(ARGUS), items, limit=5)["results"]}
    assert verdicts[items[0]["url"]]["passed"] is True
    assert "off_query" in verdicts[items[1]["url"]]["reasons"]


def test_company_event_words_beside_a_name_route_to_news():
    assert infer_vertical(ARGUS, "month")[0] == "news"
    assert infer_vertical("Harbour Mining layoffs")[0] == "news"
    assert infer_vertical("how layoffs affect morale")[0] != "news"      # no name: not a company story


def rss(items):
    body = "".join(f"<item><title>{title}</title><link>{link}</link><description>{title}</description>"
                   f"<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate></item>" for title, link in items)
    return httpx.Response(200, text=f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>',
                          headers={"content-type": "application/rss+xml"})


async def test_a_weak_boolean_answer_is_retried_with_the_name_and_the_better_answer_kept(tmp_path):
    asked = []

    def handle(request):
        query = parse_qs(urlsplit(str(request.url)).query).get("q", [""])[0]
        asked.append((request.url.host, request.url.path, query))
        if request.url.host == "127.0.0.1":
            return httpx.Response(503, text="down")
        if request.url.host == "www.bing.com" and request.url.path == "/news/search":
            if query.startswith('"Harbour Mining"') and " OR " not in query:
                return rss([(f"Harbour Mining story {n}", f"https://news{n}.example.com/2026/10/harbour-mining-{n}")
                            for n in range(5)])
            return rss([("Unrelated mining roundup", "https://news.example.com/2026/10/roundup")])
        if request.url.host == "news.google.com":
            return rss([])
        return httpx.Response(404, text="none")
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search(ARGUS, limit=5, recency="month",
                                  policy=WebPolicy(provider="http", search_good_results=3))
    note = result["receipt"]["query_simplified"]
    assert note["to"] == '"Harbour Mining"' and note["kept"] == "simpler"
    assert result["query"] == ARGUS and len(result["results"]) >= 3
    assert all("harbour-mining" in item["url"] for item in result["results"])
    assert result["receipt"]["search_routing"]["vertical"] == "news"


async def test_an_explicit_source_is_never_retried(tmp_path):
    def handle(request):
        return rss([("Unrelated", "https://news.example.com/2026/10/x")])
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search(ARGUS, source="bing_news_rss", limit=5, policy=WebPolicy(provider="http"))
    assert "query_simplified" not in result["receipt"]


# --- Google News (free) ----------------------------------------------------------------------

def test_google_news_query_carries_the_window_and_region():
    url, _ = build_search('"Harbour Mining"', "google_news_rss", {"recency": "week", "region": "en-AU"})
    params = parse_qs(urlsplit(url).query)
    assert url.startswith("https://news.google.com/rss/search?")
    assert params["q"] == ['"Harbour Mining" when:7d'] and params["gl"] == ["AU"] and params["ceid"] == ["AU:en"]


def test_google_news_items_name_their_publisher_and_date():
    content = ('<?xml version="1.0"?><rss version="2.0"><channel><item><title>Harbour Mining buys a port - '
               'Example Times</title><link>https://news.google.com/rss/articles/CBMiAB</link>'
               '<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate><description>&lt;a href="x"&gt;Harbour Mining buys'
               '&lt;/a&gt;</description><source url="https://times.example.com">Example Times</source></item>'
               '</channel></rss>')
    found, _ = normalize_results(content, None, "google_news_rss", 10, "https://news.google.com/rss/search?q=x")
    assert found[0]["title"] == "Harbour Mining buys a port"
    assert found[0]["extra"] == {"publisher": "Example Times", "publisher_url": "https://times.example.com"}
    assert found[0]["published"].startswith("2026-10-12")


# --- keyed sources ---------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    calls, routes = [], {}

    def handle(request):
        calls.append(request)
        for prefix, responder in routes.items():
            if str(request.url).startswith(prefix):
                return responder(request)
        return httpx.Response(404, text="no mock")
    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(handle))
    return calls, routes


def no_target(request):
    raise AssertionError("a search API, not the target, answers")


async def keyed_search(tmp_path, source, **kwargs):
    async with Runtime(tmp_path, transport=httpx.MockTransport(no_target)) as web:
        return await web.search("harbour mining", source=source, policy=WebPolicy(allow_paid_fallbacks=True),
                                limit=5, **kwargs)


async def test_serper_sends_the_key_window_and_region_and_prices_by_credit(tmp_path, monkeypatch, api):
    calls, routes = api
    monkeypatch.setenv("SERPER_API_KEY", KEY)
    routes["https://google.serper.dev/search"] = lambda request: httpx.Response(200, json={
        "organic": [{"title": "Harbour Mining", "link": "https://harbour.example.com/", "snippet": "Miner",
                     "position": 1}], "credits": 1})
    result = await keyed_search(tmp_path, "serper", recency="week", region="en-AU")
    body = json.loads(calls[0].content)
    assert calls[0].headers["x-api-key"] == KEY and KEY not in json.dumps(result)
    assert body.pop("num") >= 5      # the runtime asks for more than it keeps, to check and rank
    assert body == {"q": "harbour mining", "tbs": "qdr:w", "gl": "au", "hl": "en"}
    assert result["results"][0]["url"] == "https://harbour.example.com/"
    assert result["receipt"]["cost_usd"] == 0.001


async def test_serper_news_rows_come_from_its_news_endpoint(tmp_path, monkeypatch, api):
    calls, routes = api
    monkeypatch.setenv("SERPER_API_KEY", KEY)
    routes["https://google.serper.dev/news"] = lambda request: httpx.Response(200, json={"news": [
        {"title": "Harbour Mining buys a port", "link": "https://times.example.com/a", "snippet": "Deal",
         "date": "2 days ago", "source": "Example Times"}]})
    async with Runtime(tmp_path, transport=httpx.MockTransport(no_target)) as web:
        result = await web.search("Harbour Mining acquisition", source="serper", vertical="news",
                                  policy=WebPolicy(allow_paid_fallbacks=True))
    assert calls[0].url.path == "/news"
    assert result["results"][0]["extra"] == {"publisher": "Example Times"}


async def test_serpapi_sends_the_key_as_a_parameter_and_reads_organic_results(tmp_path, monkeypatch, api):
    calls, routes = api
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    routes["https://serpapi.com/search.json"] = lambda request: httpx.Response(200, json={
        "search_metadata": {"status": "Success"},
        "organic_results": [{"title": "Harbour Mining", "link": "https://harbour.example.com/", "snippet": "Miner"}]})
    result = await keyed_search(tmp_path, "serpapi", recency="month")
    params = calls[0].url.params
    assert params["engine"] == "google" and params["tbs"] == "qdr:m" and params["api_key"] == KEY
    assert KEY not in json.dumps(result["receipt"])
    assert result["results"][0]["url"] == "https://harbour.example.com/" and result["receipt"]["cost_usd"] == 0.015


async def test_google_cse_needs_a_key_and_an_engine_id(tmp_path, monkeypatch, api):
    calls, routes = api
    published = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")  # inside the week
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", KEY)
    from frankensurf.search_plugins import HostedSearchPlugin
    plugin = HostedSearchPlugin("google_cse", "google_cse_api", "google_cse_api_key", "google_cse_cx")
    assert plugin.available() is False                      # a key alone: no engine to search
    monkeypatch.setenv("FRANKENSURF_GOOGLE_CSE_CX", "engine-123")
    assert plugin.available() is True
    routes["https://www.googleapis.com/customsearch/v1"] = lambda request: httpx.Response(200, json={
        "queries": {}, "items": [{"title": "Harbour Mining", "link": "https://harbour.example.com/",
                                  "snippet": "Miner", "pagemap": {"metatags": [
                                      {"article:published_time": f"{published}T00:00:00Z"}]}}]})
    result = await keyed_search(tmp_path, "google_cse", recency="week", site="harbour.example.com")
    params = calls[0].url.params
    assert params["cx"] == "engine-123" and params["dateRestrict"] == "w1"
    assert params["siteSearch"] == "harbour.example.com" and "site:" not in params["q"]
    assert result["results"][0]["published"].startswith(published)
    assert result["receipt"]["cost_usd"] == 0.005


def test_google_cse_with_no_items_is_an_empty_answer_not_a_broken_one():
    from frankensurf.search_plugins import HostedSearchPlugin
    plugin = HostedSearchPlugin("google_cse", "google_cse_api", "google_cse_api_key", "google_cse_cx")
    assert plugin._normalize({"searchInformation": {"totalResults": "0"}}, 10) == []
    with pytest.raises(ValueError):
        plugin._normalize({"error": "bad"}, 10)
