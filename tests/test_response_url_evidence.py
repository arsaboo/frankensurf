"""Response identity remains explicit when acquisition fails after a redirect."""
import json
from types import SimpleNamespace
import sys

import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf import experimental, provider_worker
from frankensurf.runtime import WebFailure

REQUESTED = "https://www.gumtree.com.au/web/listing/lenses/12345"
FINAL = "https://www.gumtree.com.au/web/listing/lenses/99999"


@pytest.mark.parametrize("status", [404, 410])
async def test_http_redirected_not_found_retains_final_url_without_browser_fallback(tmp_path, monkeypatch, status):
    monkeypatch.setattr(experimental, "installed", lambda _: False)
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if str(request.url) == REQUESTED:
            return httpx.Response(302, headers={"location": FINAL})
        return httpx.Response(status)
    browser_calls = []
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        # A plain fetch's 404 gets one confirming read: sites fake 404s to bots.
        async def confirms(url, policy, provider):
            browser_calls.append(provider)
            raise WebFailure("NOT_FOUND", "confirmed", status, response_url=FINAL)
        web._get_browser = confirms
        # Rescuing a wrong URL through the site's search is tested elsewhere (test_rescue.py).
        result = await web.read(REQUESTED, policy_overrides={"rescue_not_found": False})
    assert calls == [REQUESTED, FINAL] and browser_calls[:1] == ["local"]
    assert result["url"] == FINAL
    assert result["receipt"]["requested_url"] == REQUESTED
    assert result["receipt"]["final_url"] == FINAL
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"
    assert result["receipt"]["http_status"] == status
    attempts = result["receipt"]["attempts"]
    assert attempts[0]["provider"] == "http" and attempts[-1]["failure"] == "NOT_FOUND"
    assert result["receipt"]["attempts"][0]["final_url"] == FINAL
    assert result["content"] == "" and result["receipt"]["evidence"] == []
    trace = json.loads((tmp_path/"traces"/(result["receipt"]["trace_id"]+".json")).read_text())
    assert trace["url"] == FINAL and trace["receipt"]["final_url"] == FINAL


@pytest.mark.parametrize("provider", ["camoufox", "scrapling", "scrapling_http"])
async def test_optional_failure_envelope_preserves_response_identity(tmp_path, monkeypatch, provider):
    calls = []
    async def packet(url, policy, chosen):
        calls.append(chosen)
        return provider_worker._result(FINAL, "", "text/html", 404)
    monkeypatch.setattr(experimental, "_packet", packet)
    async with Runtime(tmp_path) as web:
        async def unexpected(*args): raise AssertionError("NOT_FOUND must not execute another provider")
        web._get_http = web._get_browser = unexpected
        result = await web.read(REQUESTED, WebPolicy(provider=provider))
    assert calls == [provider]
    assert result["url"] == FINAL
    assert result["receipt"]["final_url"] == FINAL
    assert result["receipt"]["requested_url"] == REQUESTED
    assert result["receipt"]["http_status"] == 404
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"
    assert len(result["receipt"]["attempts"]) == 1


async def test_scrapling_redirected_not_found_crosses_worker_and_runtime_boundary(tmp_path, monkeypatch):
    class FakeSession:
        def __init__(self, **options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def fetch(self, url):
            return SimpleNamespace(status=404, url=FINAL)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", SimpleNamespace(AsyncStealthySession=FakeSession))
    monkeypatch.setattr(provider_worker, "_scrapling_safety", lambda _: None)
    async def packet(url, policy, provider):
        return await provider_worker.acquire({"provider":provider,"url":url,
            "timeout_seconds":1,"max_bytes":50000,"settle_ms":0})
    monkeypatch.setattr(experimental, "_packet", packet)
    async with Runtime(tmp_path) as web:
        result = await web.read(REQUESTED, WebPolicy(provider="scrapling"))
    assert result["url"] == FINAL and result["receipt"]["final_url"] == FINAL
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"
    assert result["receipt"]["http_status"] == 404


async def test_core_browser_status_failure_preserves_final_url(tmp_path, monkeypatch):
    async def noop(*args, **kwargs): pass
    async def goto(*args, **kwargs): return SimpleNamespace(status=404)
    page = SimpleNamespace(url=FINAL, goto=goto, close=noop)
    async def new_page(): return page
    context = SimpleNamespace(new_page=new_page, close=noop)
    async def new_context(): return context
    browser = SimpleNamespace(new_context=new_context)
    async with Runtime(tmp_path) as web:
        async def owned_browser(*args): return browser
        web._browser = owned_browser
        result = await web.read(REQUESTED, WebPolicy(provider="local"))
    assert result["url"] == FINAL and result["receipt"]["final_url"] == FINAL
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("unsafe", [
    "https://private-user:private-pass@www.gumtree.com.au/web/listing/lenses/99999",
    "https://@www.gumtree.com.au/web/listing/lenses/99999",
    "file:///private-file",
    "https://[invalid-host",
    "https://www.gumtree.com.au:invalid/web/listing/lenses/99999",
    123,
    "",
])
@pytest.mark.parametrize("packet_status", ["failed", "ok"])
async def test_invalid_final_url_never_enters_result_or_receipt(tmp_path, monkeypatch, unsafe, packet_status):
    async def packet(*args):
        return {"status":packet_status,"failure":"NOT_FOUND","http_status":404,
            "url":unsafe,"content":"private response","content_type":"text/plain"}
    monkeypatch.setattr(experimental, "_packet", packet)
    async with Runtime(tmp_path) as web:
        result = await web.read(REQUESTED, WebPolicy(provider="scrapling"))
    assert result["receipt"]["failure"]["code"] == "INVALID_URL"
    assert result["url"] == REQUESTED and result["receipt"]["final_url"] is None
    assert result["content"] == "" and result["receipt"]["evidence"] == []
    assert "private-user" not in json.dumps(result) and "private-pass" not in json.dumps(result)
    for trace in (tmp_path/"traces").glob("*.json"):
        assert "private-pass" not in trace.read_text()


def test_worker_and_typed_failure_reject_credential_url_without_emitting_it():
    unsafe = "https://private-user:private-pass@www.gumtree.com.au/web/listing/lenses/99999"
    assert provider_worker._result(unsafe, "", "text/html", 404) == {"status":"failed","failure":"INVALID_URL"}
    with pytest.raises(WebFailure) as error:
        WebFailure("NOT_FOUND", "No listing", 404, response_url=unsafe)
    assert error.value.code == "INVALID_URL" and error.value.response_url is None
    assert "private-pass" not in str(error.value)


async def test_preflight_failure_does_not_invent_final_response_url(tmp_path, monkeypatch):
    async def packet(*args): return {"status":"failed","failure":"PROVIDER_UNAVAILABLE"}
    monkeypatch.setattr(experimental, "_packet", packet)
    async with Runtime(tmp_path) as web:
        result = await web.read(REQUESTED, WebPolicy(provider="scrapling"))
    assert result["receipt"]["requested_url"] == REQUESTED
    assert result["receipt"]["final_url"] is None and result["url"] == REQUESTED


async def test_successful_redirect_records_requested_and_final_url(tmp_path):
    def handle(request):
        if str(request.url) == REQUESTED: return httpx.Response(302, headers={"location": FINAL})
        return httpx.Response(200, json={"observed":True})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.read(REQUESTED, WebPolicy(provider="http"))
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["requested_url"] == REQUESTED
    assert result["receipt"]["final_url"] == FINAL and result["url"] == FINAL


@pytest.mark.parametrize("invalid", [
    "https://fake-request-user:fake-request-password@www.gumtree.com.au/web/listing/lenses/12345",
    "file:///fake-private-input-path",
    "https://[fake-malformed-input",
])
async def test_invalid_requested_url_is_absent_from_result_stored_and_returned_trace(tmp_path, invalid):
    async with Runtime(tmp_path) as web:
        result = await web.read(invalid, WebPolicy(provider="http"))
        returned_trace = web.trace(result["receipt"]["trace_id"])
    stored_trace = (tmp_path/"traces"/(result["receipt"]["trace_id"]+".json")).read_text()
    assert result["receipt"]["failure"]["code"] == "INVALID_URL"
    assert result["url"] is None
    assert result["receipt"]["requested_url"] is None and result["receipt"]["final_url"] is None
    for serialized in (json.dumps(result), stored_trace, json.dumps(returned_trace)):
        assert "fake-request-user" not in serialized and "fake-request-password" not in serialized
        assert "fake-private-input-path" not in serialized and "fake-malformed-input" not in serialized


def enrolled_identity(tmp_path):
    from frankensurf.identity import IdentityRegistry
    path=tmp_path/"identities.json"
    registry=IdentityRegistry(path)
    registry.enroll_executor("desktop", endpoint="http://127.0.0.1:9331",
        user_data_dir=str(tmp_path/"owned-profile"), profile_ref="personal-profile")
    registry.enroll_identity("personal", executor_id="desktop", domains=["www.gumtree.com.au"])
    return path


async def bind_identity_stub(web, response):
    async def context(*args): return object()
    async def health(*args): return "verified"
    web._identity_context = context
    web._identity_health = health
    web._get_identity_browser = response


@pytest.mark.parametrize("code,status", [
    ("BLOCKED",403), ("NOT_FOUND",404), ("NOT_FOUND",410),
    ("RATE_LIMITED",429), ("CAPTCHA",200), ("IDENTITY_EXECUTOR_OFFLINE",None),
])
async def test_named_failure_urls_redacted_in_result_stored_and_returned_trace(tmp_path, code, status):
    request_url=REQUESTED+"?auth=fake-request-token#fake-request-fragment"
    final_url="https://www.gumtree.com.au/login?access_token=fake-final-token#fake-final-fragment"
    identity_registry=enrolled_identity(tmp_path)
    async def failed(url, *args):
        assert url == request_url  # Internal navigation still receives the full URL.
        raise WebFailure(code,"Controlled failure",status,response_url=final_url)
    async with Runtime(tmp_path/"state",identity_registry=identity_registry) as web:
        await bind_identity_stub(web, failed)
        result=await web.read(request_url,WebPolicy(identity="personal"))
        returned_trace=web.trace(result["receipt"]["trace_id"])
    stored_trace=(tmp_path/"state/traces"/(result["receipt"]["trace_id"]+".json")).read_text()
    assert result["receipt"]["requested_url"] == REQUESTED
    assert result["receipt"]["failure"]["code"] == code
    assert result["receipt"]["identity"] == "personal"
    assert len(result["receipt"]["attempts"]) == 1
    for serialized in (json.dumps(result),stored_trace,json.dumps(returned_trace)):
        for token in ("fake-request-token","fake-request-fragment","fake-final-token","fake-final-fragment"):
            assert token not in serialized
    if not code.startswith("IDENTITY_"):
        assert result["url"] == "https://www.gumtree.com.au/login"
        assert result["receipt"]["final_url"] == result["url"]
        assert result["receipt"]["attempts"][0]["final_url"] == result["url"]


async def test_named_success_cache_and_legacy_trace_redact_urls_without_changing_execution(tmp_path):
    request_url=REQUESTED+"?auth=fake-request-token#fake-request-fragment"
    final_url=FINAL+"?access_token=fake-final-token#fake-final-fragment"
    identity_registry=enrolled_identity(tmp_path)
    calls=[]
    async def observed(url,*args):
        calls.append(url)
        content="<title>Current lens</title><p>Current description</p>"
        return {"url":final_url,"content":content,"raw":content.encode(),
            "content_type":"text/html","headers":{},"http_status":200}
    async with Runtime(tmp_path/"state",identity_registry=identity_registry) as web:
        await bind_identity_stub(web,observed)
        result=await web.read(request_url,WebPolicy(identity="personal"))
        cached=await web.read(request_url,WebPolicy(identity="personal",freshness="hour"))
        returned_trace=web.trace(result["receipt"]["trace_id"])
        assert calls == [request_url]
        assert cached["receipt"]["cache_hit"] is True
        for output in (result,cached,returned_trace):
            assert output["url"] == FINAL
            assert output["receipt"]["requested_url"] == REQUESTED
            assert output["receipt"]["final_url"] == FINAL
        for artifact in (tmp_path/"state").rglob("*.json"):
            serialized=artifact.read_text()
            assert "fake-request-token" not in serialized and "fake-final-token" not in serialized
            assert "fake-request-fragment" not in serialized and "fake-final-fragment" not in serialized
        # Old trace/cache records are sanitized on output as well as new writes.
        trace_path=tmp_path/"state/traces"/(result["receipt"]["trace_id"]+".json")
        legacy=json.loads(trace_path.read_text())
        legacy["url"]=final_url
        legacy["receipt"]["requested_url"]=request_url
        legacy["receipt"]["final_url"]=final_url
        legacy["receipt"]["attempts"][0]["final_url"]=final_url
        trace_path.write_text(json.dumps(legacy))
        returned_legacy=web.trace(result["receipt"]["trace_id"])
        assert "fake-final-token" not in json.dumps(returned_legacy)
        assert "fake-request-token" not in json.dumps(returned_legacy)
        for path in (tmp_path/"state/cache").glob("*.json"):
            path.write_text(json.dumps(legacy))
        legacy_cached=await web.read(request_url,WebPolicy(identity="personal",freshness="hour"))
        assert legacy_cached["receipt"]["cache_hit"] is True
        assert "fake-final-token" not in json.dumps(legacy_cached)
        assert "fake-request-token" not in json.dumps(legacy_cached)
        cached_trace=tmp_path/"state/traces"/(legacy_cached["receipt"]["trace_id"]+".json")
        assert "fake-final-token" not in cached_trace.read_text()
