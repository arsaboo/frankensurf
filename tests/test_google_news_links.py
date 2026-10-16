"""Google News RSS links are swapped for the publisher's URL (synthetic Google responses)."""
import asyncio
import json

import httpx

from frankensurf.search import resolve_google_news

ARTICLE = "https://news.google.com/rss/articles/CBMiAbc123-_x?oc=5"


def google(target="https://publisher.example/2026/10/story", signed=True, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path, request.headers.get("user-agent")))
        if request.method == "GET":
            attrs = 'data-n-a-sg="SIG" data-n-a-ts="1760000000"' if signed else ""
            return httpx.Response(200, text=f"<html><c-wiz><div jscontroller=\"x\" {attrs}></div></c-wiz></html>")
        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        inner = json.loads(json.loads(httpx.QueryParams(f"f.req={form['f.req']}")["f.req"])[0][0][1])
        assert inner[2] == "CBMiAbc123-_x" and inner[3] == 1760000000 and inner[4] == "SIG"
        body = json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", target, 1])]])
        return httpx.Response(200, text=")]}'\n\n" + body)
    return httpx.MockTransport(handler)


def test_google_link_becomes_the_publisher_url_and_the_original_is_kept():
    seen = []
    results = [{"url": ARTICLE, "title": "Story"}, {"url": "https://other.example/a", "title": "Other"}]
    asyncio.run(resolve_google_news(results, "FrankenSurf/test", transport=google(seen=seen)))
    assert results[0]["url"] == "https://publisher.example/2026/10/story"
    assert results[0]["extra"]["google_news_url"] == ARTICLE
    assert results[1] == {"url": "https://other.example/a", "title": "Other"}
    assert [method for method, _, _ in seen] == ["GET", "POST"]
    assert all(agent == "FrankenSurf/test" for _, _, agent in seen)


def test_a_link_that_does_not_decode_stays_as_it_was():
    results = [{"url": ARTICLE}]
    asyncio.run(resolve_google_news(results, "FrankenSurf/test", transport=google(signed=False)))
    assert results == [{"url": ARTICLE}]


def test_a_target_that_is_not_a_web_url_is_ignored():
    results = [{"url": ARTICLE}]
    asyncio.run(resolve_google_news(results, "FrankenSurf/test", transport=google(target="javascript:alert(1)")))
    assert results == [{"url": ARTICLE}]


def test_no_google_links_means_no_requests():
    seen = []
    results = [{"url": "https://example.com/a"}]
    asyncio.run(resolve_google_news(results, "FrankenSurf/test", transport=google(seen=seen)))
    assert seen == []
