import asyncio
import json

import httpx

from frankensurf import firecrawl_compat
from frankensurf.firecrawl_compat import FirecrawlCompat


def observed(url, text="Menu Home About\n" + "Example article body " * 20):
    main = "Example article body " * 20 if "article" in url else "Result one"
    return {"url": url, "title": "Example", "text": text, "main_text": main,
            "content": "<html><body>Example</body></html>",
            "receipt": {"status": "observed", "trace_id": "t-ok", "http_status": 200,
                        "attempts": [{"provider": "http"}], "cost_usd": 0}}


def walled(url):
    return {"url": url, "text": "", "receipt": {
        "status": "failed", "trace_id": "t-wall",
        "failure": {"code": "CAPTCHA", "message": "Provider returned a challenge or sign-in page"},
        "attempts": [{"provider": "http"}, {"provider": "camoufox"}, {"provider": "camoufox"}],
        "next_step": {"provider": "handoff"}}}


class FakeWeb:
    def __init__(self, delay=0.0):
        self.reads, self.delay = [], delay

    async def read(self, url, policy_overrides=None):
        self.reads.append((url, policy_overrides))
        await asyncio.sleep(self.delay)
        return walled(url) if "walled" in url else observed(url)

    async def search(self, query, limit=10, policy=None):
        self.search_policy = policy
        return {"results": [{"url": "https://example.com/a", "title": "A", "snippet": "about a"},
                            {"url": "https://walled.example/b", "title": "B", "snippet": "about b"},
                            {"url": "https://example.com/c", "title": "C", "snippet": "about c"}][:limit]}


def call(app, path, body, method="POST"):
    return asyncio.run(app.handle(method, path, json.dumps(body).encode()))


def test_scrape_returns_markdown_and_metadata_in_firecrawl_shape():
    web = FakeWeb()
    status, out = call(FirecrawlCompat(web), "/v1/scrape", {"url": "https://example.com/article",
                                                             "formats": ["markdown", "html"]})
    assert status == 200 and out["success"] is True
    assert out["data"]["markdown"].startswith("Menu Home About")
    assert out["data"]["html"].startswith("<html>")
    assert out["data"]["metadata"]["sourceURL"] == "https://example.com/article"
    assert out["data"]["metadata"]["frankensurf"]["trace_id"] == "t-ok"
    assert web.reads[0][1]["prefer_markdown"] is True


def test_only_main_content_is_ignored_and_the_whole_page_comes_back():
    # The article cut lost search and listing results; Firecrawl's SDKs send true by default.
    web = FakeWeb()
    status, out = call(FirecrawlCompat(web), "/v1/scrape",
                       {"url": "https://example.com/search?q=x", "onlyMainContent": True})
    assert out["data"]["markdown"].startswith("Menu Home About")
    assert "main_content" not in web.reads[0][1]


def test_scrape_of_a_page_no_tool_could_read_says_so_and_returns_no_page():
    status, out = call(FirecrawlCompat(FakeWeb()), "/v1/scrape", {"url": "https://walled.example/x"})
    assert status == 500  # what Firecrawl answers when no engine got the page; SDKs surface the error
    assert out["success"] is False and "data" not in out
    assert out["error"].startswith("Could not read this page (CAPTCHA")
    assert "Tried: http, camoufox." in out["error"]
    assert "Don't fill in its contents" in out["error"]
    assert out["frankensurf"]["tried"] == ["http", "camoufox"]


def test_search_leaves_out_markdown_for_results_it_could_not_read():
    status, out = call(FirecrawlCompat(FakeWeb()), "/v1/search",
                       {"query": "example", "limit": 3, "scrapeOptions": {"formats": ["markdown"]}})
    by_url = {item["url"]: item for item in out["data"]}
    assert out["success"] is True and len(out["data"]) == 3
    assert by_url["https://example.com/a"]["markdown"]
    assert "markdown" not in by_url["https://walled.example/b"]
    assert by_url["https://walled.example/b"]["metadata"]["error"].startswith("Could not read this page")
    assert by_url["https://walled.example/b"]["description"] == "about b"


def test_search_without_scrape_options_reads_nothing_and_v2_nests_under_web():
    web = FakeWeb()
    status, out = call(FirecrawlCompat(web), "/v2/search", {"query": "example", "limit": 2})
    assert [item["url"] for item in out["data"]["web"]] == ["https://example.com/a", "https://walled.example/b"]
    assert web.reads == []
    assert web.search_policy.provider == "http"  # search pages themselves load over plain HTTP by default


def test_request_timeout_is_a_reported_failure_not_a_page():
    status, out = call(FirecrawlCompat(FakeWeb(delay=2)), "/v1/scrape",
                       {"url": "https://example.com/slow", "timeout": 1000})
    assert status == 408 and out["success"] is False and "TIMEOUT" in out["error"]


def test_short_client_timeouts_keep_most_of_the_time_for_reading():
    assert firecrawl_compat.read_budget(30000) == 27.0   # 3 s early
    assert firecrawl_compat.read_budget(7500) == 6.0     # LibreChat's default: a fifth early, not 3 s
    assert firecrawl_compat.read_budget(500) == 1.0


def test_v2_object_formats_count_like_their_names():
    # n8n and Firecrawl's v2 docs send formats as objects: {"type": "markdown"}
    status, out = call(FirecrawlCompat(FakeWeb()), "/v2/scrape", {"url": "https://example.com/article",
                       "formats": [{"type": "markdown"}, {"type": "html"}, {"type": "links"}]})
    assert status == 200 and out["data"]["markdown"] and out["data"]["html"].startswith("<html>")
    assert "links" in out["data"]
    assert firecrawl_compat._format_names(["markdown", {"type": "rawHtml"}, {"no": "type"}, 3]) == ["markdown", "rawHtml"]


def test_account_endpoints_answer_so_key_checks_pass():
    # n8n's Firecrawl credential test calls credit-usage; the SDKs read these shapes.
    app = FirecrawlCompat(FakeWeb())
    status, out = call(app, "/v2/team/credit-usage", {}, method="GET")
    assert status == 200 and out["success"] is True and out["data"]["remainingCredits"] > 0
    assert call(app, "/v1/team/token-usage", {}, method="GET")[1]["data"]["remainingTokens"] > 0
    assert call(app, "/v2/team/credit-usage/historical", {}, method="GET")[1]["periods"] == []
    queue = call(app, "/v2/team/queue-status", {}, method="GET")[1]
    assert queue["jobsInQueue"] == 0 and queue["maxConcurrency"] > 0
    assert call(app, "/v2/concurrency-check", {}, method="GET")[1]["concurrency"] == 0
    assert call(app, "/v2/team/billing", {}, method="GET")[0] == 404


def test_bad_requests_and_unknown_routes():
    app = FirecrawlCompat(FakeWeb())
    assert call(app, "/v1/scrape", {"url": "file:///etc/passwd"})[0] == 400
    assert call(app, "/v1/search", {"query": " "})[0] == 400
    assert call(app, "/v1/deep-research", {"url": "https://example.com"})[0] == 404
    assert asyncio.run(app.handle("POST", "/v1/scrape", b"not json"))[0] == 400
    assert asyncio.run(app.handle("GET", "/v1/scrape", b""))[0] == 405
    assert asyncio.run(app.handle("GET", "/health", b""))[1]["service"] == "frankensurf"


def test_serve_answers_over_http(monkeypatch):
    class FakeRuntime(FakeWeb):
        def __init__(self, state_dir=None, **kwargs):
            super().__init__()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(firecrawl_compat, "Runtime", FakeRuntime)

    async def scenario():
        started = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(firecrawl_compat.serve(None, "127.0.0.1", 0, ready=started.set_result))
        server = await started
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            ok = await client.post(f"http://127.0.0.1:{port}/v1/scrape", json={"url": "https://example.com/article"},
                                   headers={"Authorization": "Bearer anything"})
            missing = await client.post(f"http://127.0.0.1:{port}/v0/nothing", json={})
        task.cancel()
        return ok, missing

    ok, missing = asyncio.run(scenario())
    assert ok.status_code == 200 and ok.json()["data"]["markdown"]
    assert missing.status_code == 404


def test_serve_search_asks_the_engine_named_in_the_environment(monkeypatch, tmp_path):
    # Measured: serve searched the public engines while FRANKENSURF_SEARCH_URL pointed at a SearXNG.
    from frankensurf import Runtime
    monkeypatch.setenv("FRANKENSURF_SEARCH_URL", "http://192.168.2.162:4002")
    asked = []

    def answer(request):
        asked.append(str(request.url))
        if request.url.host == "192.168.2.162":
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/%d" % index, "title": "example %d" % index,
                 "content": "example thing %d" % index} for index in range(6)]})
        return httpx.Response(200, json={"results": []})

    async def scenario():
        async with Runtime(tmp_path, transport=httpx.MockTransport(answer)) as web:
            status, out = await FirecrawlCompat(web).handle("POST", "/v1/search", json.dumps({"query": "example"}).encode())
        return status, out, asked

    status, out, asked = asyncio.run(scenario())
    assert status == 200 and out["success"] is True
    assert any("192.168.2.162:4002/search?" in url for url in asked)
