"""Hosted plugins against mocked APIs: wiring and contracts, not live access."""
import json

import httpx
import pytest

from frankensurf import hosted_providers
from frankensurf.runtime import Runtime, WebPolicy

URL = "https://shop.example.com/item/1"
HTML = "<html><head><title>Item one</title></head><body><h1>Item one</h1>" + "x" * 200 + "</body></html>"
KEY = "test-key-must-not-leak"


@pytest.fixture
def api(monkeypatch):
    calls = []
    routes = {}

    def handle(request):
        calls.append(request)
        for prefix, responder in routes.items():
            if str(request.url).startswith(prefix):
                return responder(request)
        return httpx.Response(404, text="no mock")

    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(handle))
    return calls, routes


def key(monkeypatch, name, value=KEY):
    monkeypatch.setenv("FRANKENSURF_" + name.upper(), value)


def no_target_http(request):
    raise AssertionError("Hosted provider must not fall back to direct HTTP here")


async def read(tmp_path, url, **overrides):
    async with Runtime(tmp_path, transport=httpx.MockTransport(no_target_http)) as web:
        return await web.read(url, policy_overrides=overrides)


async def test_jina_reader_needs_no_key_and_returns_markdown(tmp_path, api):
    calls, routes = api
    routes["https://r.jina.ai/"] = lambda request: httpx.Response(200, json={
        "code": 200, "data": {"title": "Item one", "url": URL, "content": "Price A$42\n\n![p](https://img.example.com/1.jpg)"}})
    result = await read(tmp_path, URL, provider="jina_reader")
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Item one"
    assert "Price A$42" in result["text"]
    assert result["image_urls"] == ["https://img.example.com/1.jpg"]
    assert str(calls[0].url) == "https://r.jina.ai/" + URL
    assert "authorization" not in calls[0].headers


async def test_keyed_plugins_are_skipped_until_a_key_exists(tmp_path, monkeypatch):
    async with Runtime(tmp_path) as web:
        assert not web.providers.is_available("firecrawl")
        assert not web.providers.is_available("zenrows")
        key(monkeypatch, "firecrawl_api_key")
        assert web.providers.is_available("firecrawl")
        result = await web.read(URL, policy_overrides={"provider": "zenrows", "allow_paid_fallbacks": True})
    assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"


async def test_paid_plugins_need_an_explicit_grant(tmp_path, monkeypatch, api):
    key(monkeypatch, "firecrawl_api_key")
    result = await read(tmp_path, URL, provider="firecrawl")
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert api[0] == []


async def test_firecrawl_scrape_returns_raw_html_and_hides_the_key(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "firecrawl_api_key")
    routes["https://api.firecrawl.dev/v2/scrape"] = lambda request: httpx.Response(200, json={
        "success": True, "data": {"rawHtml": HTML, "metadata": {"statusCode": 200, "url": URL}}})
    result = await read(tmp_path, URL, provider="firecrawl", allow_paid_fallbacks=True)
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Item one"
    sent = json.loads(calls[0].content)
    assert sent == {"url": URL, "formats": ["rawHtml"]}
    assert calls[0].headers["authorization"] == "Bearer " + KEY
    assert KEY not in json.dumps(result, default=str)


async def test_firecrawl_reports_target_and_account_failures(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "firecrawl_api_key")
    routes["https://api.firecrawl.dev/v2/scrape"] = lambda request: httpx.Response(200, json={
        "success": True, "data": {"rawHtml": "gone", "metadata": {"statusCode": 404, "url": URL}}})
    missing = await read(tmp_path / "a", URL, provider="firecrawl", allow_paid_fallbacks=True)
    routes["https://api.firecrawl.dev/v2/scrape"] = lambda request: httpx.Response(402, json={"error": "credits"})
    broke = await read(tmp_path / "b", URL, provider="firecrawl", allow_paid_fallbacks=True)
    assert missing["receipt"]["failure"]["code"] == "NOT_FOUND"
    assert broke["receipt"]["failure"]["code"] == "BUDGET_EXHAUSTED"


async def test_zenrows_unblocker_passes_render_options(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "zenrows_api_key")
    routes["https://api.zenrows.com/v1/"] = lambda request: httpx.Response(
        200, text=HTML, headers={"content-type": "text/html", "zr-final-url": URL + "?ok=1"})
    result = await read(tmp_path, URL, provider="zenrows", allow_paid_fallbacks=True)
    assert result["receipt"]["status"] == "observed"
    assert result["url"] == URL + "?ok=1"
    params = calls[0].url.params
    assert params["url"] == URL and params["js_render"] == "true" and params["apikey"] == KEY
    assert KEY not in json.dumps(result, default=str)


async def test_zenrows_mirrors_target_blocks(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "zenrows_api_key")
    routes["https://api.zenrows.com/v1/"] = lambda request: httpx.Response(
        403, text="<html>denied</html>", headers={"content-type": "text/html"})
    result = await read(tmp_path, URL, provider="zenrows", allow_paid_fallbacks=True)
    assert result["receipt"]["failure"]["code"] == "BLOCKED"


async def test_scrapfly_reads_result_content(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "scrapfly_api_key")
    routes["https://api.scrapfly.io/scrape"] = lambda request: httpx.Response(200, json={
        "result": {"content": HTML, "status_code": 200, "url": URL, "content_type": "text/html"},
        "context": {"cost": 5}})
    result = await read(tmp_path, URL, provider="scrapfly", allow_paid_fallbacks=True)
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Item one"
    assert calls[0].url.params["unblocker"] == "true"


async def test_brightdata_needs_key_and_zone(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "brightdata_api_key")
    routes["https://api.brightdata.com/request"] = lambda request: httpx.Response(
        200, text=HTML, headers={"content-type": "text/html"})
    without_zone = await read(tmp_path / "a", URL, provider="brightdata_unlocker", allow_paid_fallbacks=True)
    key(monkeypatch, "brightdata_zone", "unlocker1")
    with_zone = await read(tmp_path / "b", URL, provider="brightdata_unlocker", allow_paid_fallbacks=True)
    assert without_zone["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"
    assert with_zone["receipt"]["status"] == "observed"
    assert json.loads(calls[-1].content) == {"zone": "unlocker1", "url": URL, "format": "raw"}


async def test_brightdata_refusal_is_not_a_page(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "brightdata_api_key")
    key(monkeypatch, "brightdata_zone", "unlocker1")
    routes["https://api.brightdata.com/request"] = lambda request: httpx.Response(
        200, text="Residential Failed (bad_endpoint): Requested site is not available for immediate "
                  "residential (no KYC) access mode in accordance with robots.txt.",
        headers={"content-type": "text/plain"})
    by_body = await read(tmp_path / "a", URL, provider="brightdata_unlocker", allow_paid_fallbacks=True)
    routes["https://api.brightdata.com/request"] = lambda request: httpx.Response(
        200, text="", headers={"content-type": "text/html", "x-brd-error": "Target site blocked"})
    by_header = await read(tmp_path / "b", URL, provider="brightdata_unlocker", allow_paid_fallbacks=True)
    for result in (by_body, by_header):
        assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"
    assert "bad_endpoint" in by_body["receipt"]["failure"]["message"]


async def test_unblockers_join_default_routing_only_with_key_and_paid_grant(tmp_path, monkeypatch):
    from frankensurf.runtime import WebPolicy
    async with Runtime(tmp_path) as web:
        free = web.providers.candidates(WebPolicy())
        key(monkeypatch, "zenrows_api_key")
        granted = web.providers.candidates(WebPolicy(allow_paid_fallbacks=True))
    assert "zenrows" not in free and "jina_reader" in free
    assert "zenrows" in granted
    assert "exa_api" not in granted and "brave_api" not in granted


async def test_exa_search_returns_results_and_cost(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "exa_api_key")
    routes["https://api.exa.ai/search"] = lambda request: httpx.Response(200, json={
        "results": [{"title": "Sony A7 III review", "url": "https://cams.example.com/a7iii",
                     "publishedDate": "2026-09-01", "highlights": ["Great sensor"]}],
        "costDollars": {"total": 0.005}})
    async with Runtime(tmp_path, transport=httpx.MockTransport(no_target_http)) as web:
        result = await web.search("sony a7iii", source="exa",
                                  policy=WebPolicy(allow_paid_fallbacks=True))
    assert result["receipt"]["status"] == "observed"
    assert result["results"][0]["url"] == "https://cams.example.com/a7iii"
    assert result["results"][0]["snippet"] == "Great sensor"
    assert result["receipt"]["cost_usd"] == 0.005
    sent = json.loads(calls[0].content)
    assert sent["query"] == "sony a7iii" and calls[0].headers["x-api-key"] == KEY
    assert KEY not in json.dumps(result, default=str)


async def test_brave_search_normalizes_web_results(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, "brave_api_key")
    routes["https://api.search.brave.com/res/v1/web/search"] = lambda request: httpx.Response(200, json={
        "web": {"results": [{"title": "A7 III", "url": "https://cams.example.com/a7iii",
                             "description": "The <strong>A7 III</strong> body", "page_age": "2026-08-01"}]}})
    async with Runtime(tmp_path, transport=httpx.MockTransport(no_target_http)) as web:
        result = await web.search("sony a7iii", source="brave",
                                  policy=WebPolicy(allow_paid_fallbacks=True))
    assert result["receipt"]["status"] == "observed"
    assert result["results"][0]["snippet"] == "The A7 III body", result["receipt"]
    assert calls[0].headers["x-subscription-token"] == KEY
    assert calls[0].url.params["q"] == "sony a7iii"


async def test_keyless_hosted_search_is_skipped_in_fallback(tmp_path):
    async with Runtime(tmp_path) as web:
        from frankensurf.runtime import WebPolicy
        assert "exa" not in web.searches.candidates(WebPolicy(allow_paid_fallbacks=True))


def test_keys_load_from_env_file_after_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# FrankenSurf keys\n"
                   "FRANKENSURF_EXA_API_KEY=from-file\n"
                   "export FRANKENSURF_ZENROWS_API_KEY=\"quoted\"\n"
                   "FRANKENSURF_SCRAPFLY_API_KEY=\n"
                   "not a setting\n")
    monkeypatch.setattr(hosted_providers, "_ENV_FILE", env)
    assert hosted_providers._setting("exa_api_key") == "from-file"
    assert hosted_providers._setting("zenrows_api_key") == "quoted"
    assert hosted_providers._setting("scrapfly_api_key") is None
    monkeypatch.setenv("FRANKENSURF_EXA_API_KEY", "from-environment")
    assert hosted_providers._setting("exa_api_key") == "from-environment"


async def test_zenrows_and_firecrawl_report_dollar_costs(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, 'zenrows_api_key')
    key(monkeypatch, 'firecrawl_api_key')
    routes['https://api.zenrows.com/v1/'] = lambda request: httpx.Response(
        200, text=HTML, headers={'content-type': 'text/html', 'x-request-cost': '0.025', 'x-request-credits': '25'})
    routes['https://api.firecrawl.dev/v2/scrape'] = lambda request: httpx.Response(200, json={
        'success': True, 'data': {'rawHtml': HTML, 'metadata': {'statusCode': 200, 'url': URL, 'creditsUsed': 2}}})
    zen = await read(tmp_path / 'a', URL, provider='zenrows', allow_paid_fallbacks=True)
    fire = await read(tmp_path / 'b', URL, provider='firecrawl', allow_paid_fallbacks=True)
    monkeypatch.setenv('FRANKENSURF_FIRECRAWL_USD_PER_CREDIT', '0.001')
    own_rate = await read(tmp_path / 'c', URL, provider='firecrawl', allow_paid_fallbacks=True)
    assert zen['receipt']['cost_usd'] == 0.025
    assert fire['receipt']['cost_usd'] == 0.0076
    assert own_rate['receipt']['cost_usd'] == 0.002


async def test_cost_cap_refuses_before_calling_and_allows_within_cap(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, 'zenrows_api_key')
    routes['https://api.zenrows.com/v1/'] = lambda request: httpx.Response(
        200, text=HTML, headers={'content-type': 'text/html', 'x-request-cost': '0.025'})
    refused = await read(tmp_path / 'a', URL, provider='zenrows', allow_paid_fallbacks=True, max_cost_usd=0.01)
    assert refused['receipt']['failure']['code'] == 'BUDGET_EXHAUSTED'
    assert calls == []
    allowed = await read(tmp_path / 'b', URL, provider='zenrows', allow_paid_fallbacks=True, max_cost_usd=0.05)
    assert allowed['receipt']['status'] == 'observed'
    assert allowed['receipt']['cost_usd'] == 0.025


async def test_charged_target_failures_keep_their_cost(tmp_path, monkeypatch, api):
    calls, routes = api
    key(monkeypatch, 'zenrows_api_key')
    routes['https://api.zenrows.com/v1/'] = lambda request: httpx.Response(
        404, text='<html>gone</html>', headers={'content-type': 'text/html', 'x-request-cost': '0.025'})
    result = await read(tmp_path, URL, provider='zenrows', allow_paid_fallbacks=True)
    assert result['receipt']['failure']['code'] == 'NOT_FOUND'
    assert result['receipt']['cost_usd'] == 0.025


async def test_archive_is_opt_in_and_says_the_page_is_not_live(tmp_path, api):
    calls, routes = api
    routes["https://archive.org/wayback/available"] = lambda request: httpx.Response(200, json={
        "archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20260901123045",
                                           "url": "http://web.archive.org/web/20260901123045/" + URL}}})
    routes["https://web.archive.org/web/20260901123045id_/"] = lambda request: httpx.Response(
        200, text=HTML, headers={"content-type": "text/html"})
    async with Runtime(tmp_path) as web:
        assert "internet_archive" not in web.providers.candidates(WebPolicy())
        assert "internet_archive" in web.providers.candidates(WebPolicy(allow_archive=True))
    denied = await read(tmp_path, URL, provider="internet_archive")
    assert denied["receipt"]["failure"]["code"] == "POLICY_DENIED" and not calls
    result = await read(tmp_path, URL, provider="internet_archive", allow_archive=True)
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and result["title"] == "Item one"
    assert receipt["archived"]["archived_at"] == "2026-09-01T12:30:45Z"
    assert receipt["source_freshness"] == "archived"
    assert result["archived"]["snapshot_url"] == "https://web.archive.org/web/20260901123045/" + URL


async def test_archive_without_a_copy_is_not_found(tmp_path, api):
    _, routes = api
    routes["https://archive.org/wayback/available"] = lambda request: httpx.Response(
        200, json={"archived_snapshots": {}})
    result = await read(tmp_path, URL, provider="internet_archive", allow_archive=True)
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"
