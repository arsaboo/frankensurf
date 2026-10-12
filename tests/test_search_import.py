from datetime import datetime,timezone,timedelta
import hashlib
from pathlib import Path
import httpx
import pytest
from frankensurf import Runtime,WebPolicy
from frankensurf.search import build_search


async def test_searx_results_are_index_candidates_with_attribution_and_partial_failures(tmp_path):
    queries=[]
    def handle(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200,json={"results":[{"url":"https://shop.test/bike","title":"Used bike","content":"2000 dollars","engines":["bing"]},{"url":"https://shop.test/bike","title":"duplicate"},{"url":"javascript:bad","title":"bad"}],"unresponsive_engines":[["brave","too many requests"]]})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle)) as web:
        result=await web.search("Sydney bike & pickup",limit=5,policy=WebPolicy(search_good_results=1))
    assert queries==["Sydney bike & pickup"]
    assert len(result["results"])==1
    assert result["coverage"]=="partial"
    hit=result["results"][0]
    assert hit["listing_state"]=="unknown" and hit["verification"]=="indexed_discovery"
    assert hit["query_attribution"]["query"]=="Sydney bike & pickup"
    assert hit["receipt"]["operation"]=="search"
    assert Path(hit["receipt"]["evidence"][0]["path"]).exists()


async def test_upstream_search_failure_is_not_proof_of_zero_inventory(tmp_path):
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"results":[],"unresponsive_engines":[["bing","captcha"]]}))) as web:
        result=await web.search("Sydney ebike")
    assert result["receipt"]["failure"]["code"]=="SEARCH_UNAVAILABLE"
    assert result["results"]==[]


async def test_bing_rss_search_normalizes_and_limits(tmp_path):
    rss='<rss><channel><item><title>Bike</title><link>https://shop.test/1</link><description>Ask 2000</description></item><item><title>Second</title><link>https://shop.test/2</link></item></channel></rss>'
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,text=rss,headers={"content-type":"application/rss+xml"}))) as web:
        result=await web.search("ebike",source="bing_rss",limit=1)
    assert len(result["results"])==1
    assert result["results"][0]["snippet"]=="Ask 2000"
    assert result["results"][0]["listing_state"]=="unknown"


def test_search_registry_rejects_paid_or_unvalidated_endpoint():
    with pytest.raises(ValueError): build_search("bike","unknown")
    with pytest.raises(ValueError): build_search("bike","searxng",{"base_url":"https://paid-provider.test"})
    with pytest.raises(ValueError): build_search("bike","bing_rss",{"base_url":"https://unvalidated.test"})


async def test_operator_import_preserves_actual_timestamp_without_live_or_automation_claim(tmp_path):
    actual=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    content='{"title":"Trek bike","description":"Visible seller description"}'
    async with Runtime(tmp_path) as web:
        result=web.import_evidence(content,"https://shop.test/bike",actual,content_type="application/json",image_urls=["https://cdn.test/photo"])
        trace=web.trace(result["receipt"]["trace_id"])
    assert result["receipt"]["method"]=="operator_browser"
    assert result["receipt"]["observed_at"]==actual
    assert result["receipt"]["freshness_seconds"]>=7200
    assert result["receipt"]["automation_verified"] is False
    assert result["receipt"]["confidence"] is None
    assert result["field_status"]["availability"]=="unknown"
    assert result["receipt"]["evidence"][0]["sha256"]==hashlib.sha256(content.encode()).hexdigest()
    assert result["image_urls"]==["https://cdn.test/photo"]
    assert "content" not in trace


async def test_operator_import_rejects_future_or_timezone_free_observation(tmp_path):
    async with Runtime(tmp_path) as web:
        with pytest.raises(ValueError): web.import_evidence("Visible", "https://shop.test", "2026-10-01T00:00:00")
        with pytest.raises(ValueError): web.import_evidence("Visible", "https://shop.test", (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())


def test_duckduckgo_search_challenge_is_typed_not_zero_results():
    from frankensurf.runtime import _challenge
    assert _challenge('<title>DuckDuckGo</title><p>Please complete the following challenge to confirm this search was made by a human.</p>')


async def test_search_survives_completeness_and_try_harder_receipt_fields(tmp_path, monkeypatch):
    # Regression: v0.16 receipts gained fields the search copy rejected, so
    # every SearXNG search failed as SCHEMA_CHANGED with completeness on.
    monkeypatch.setattr(Runtime, "completeness_enabled", True)
    queries = []
    def handle(request):
        queries.append(request.url.params.get("q"))
        return httpx.Response(200, json={"results": [{"url": "https://shop.test/bike", "title": "Used bike"}]})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("bike", limit=5, policy=WebPolicy(search_good_results=1))
    assert queries == ["bike"] and len(result["results"]) == 1
